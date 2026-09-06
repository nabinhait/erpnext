# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Cutover opening_assertions: pin a key's state at the frontier with one SLE-less
Assertion fact per (item, warehouse).

Two sources feed the same event writer:

- ``create_opening_assertions`` — freeze-the-past: pins legacy's stored balance, lots
  seeded from Serial and Batch Entries. Facts only, no GL, no repricing.
- ``get_opening_differences`` — the v17 frontier: engine truth per key next to
  legacy's stored value; the Stock Opening Adjustment owns the opening_assertions it
  emits from this and books the value difference once, in the open period.

Refolds and rebuilds never walk behind the latest active opening_assertion, so history
stays exactly as written while forward folding starts clean: a negative
balance freezes as modelled exposure settled by the next receipts, batchwise
batches are seeded as lot sub-states, quantity-tag batches ride the pool.

    bench --site <site> execute erpnext.stock.services.stock_engine_opening.create_opening_assertions \
        --kwargs "{'company': 'My Company Ltd'}"
"""

from collections.abc import Iterable

import frappe
from frappe.utils import flt

from erpnext.stock.services import stock_event_writer

EVENT_FIELDS = (
	"name",
	"item_code",
	"warehouse",
	"company",
	"posting_datetime",
	"kind",
	"qty_change",
	"declared_rate",
	"assert_qty",
	"assert_rate",
	"value_change",
	"voucher_type",
	"voucher_no",
	"source",
	"creation",
	"modified",
	"owner",
	"modified_by",
)
FLUSH_AT = 500
TOLERANCE = 1e-6


def create_opening_assertions(
	company: str, moment: str | None = None, closing_entry: str | None = None
) -> dict:
	"""Emit one opening_assertion Assertion per key of the company pinning legacy's
	stored balance, then invalidate engine state so the next fold rebuilds
	from the opening_assertion.

	With closing_entry, the opening_assertions are owned by that Stock Closing Entry:
	they lock only while it stays submitted — cancelling the closing revokes
	them (the frontier model). Without it, the freeze is unconditional."""
	moment = str(moment or frappe.utils.now_datetime())
	report = {
		"company": company,
		"moment": moment,
		"negative": 0,
		"lots_seeded": 0,
		"lot_mismatch": 0,
		"pool_residual": 0.0,
	}
	owner = ("Stock Closing Entry", closing_entry) if closing_entry else None

	def opening_assertions() -> Iterable[frappe._dict]:
		for balance in _get_legacy_closing_balances(company, moment):
			seeds = _get_lot_seeds_from_legacy(balance, report)
			if flt(balance.qty) < 0:
				report["negative"] += 1
			rate, residual = _get_unlotted_pool_rate(flt(balance.qty), flt(balance.value), seeds)
			report["pool_residual"] += residual
			yield frappe._dict(
				item_code=balance.item_code,
				warehouse=balance.warehouse,
				qty=flt(balance.qty),
				rate=rate,
				seeds=seeds,
			)

	report["keys"] = insert_opening_assertions(company, moment, opening_assertions(), owner)
	report["pool_residual"] = flt(report["pool_residual"], 4)
	return report


def get_opening_differences(company: str, moment: str) -> list[frappe._dict]:
	"""Engine truth versus legacy's stored balance for every key of the
	company at the moment. Each row carries what a opening_assertion needs (qty,
	rate, lot seeds from the fold's batch sub-states) and the value delta the
	opening adjustment books. Keys the fold cannot value (Standard Cost) are
	returned with ``skipped`` set and no delta."""
	from erpnext.stock.services import stock_engine_adapter, stock_engine_snapshots

	engine = stock_engine_adapter.get_engine()
	snapshots = stock_engine_snapshots.get_snapshot_states(engine, company, moment)
	rows = []
	for balance in _get_legacy_closing_balances(company, moment):
		row = frappe._dict(
			item_code=balance.item_code,
			warehouse=balance.warehouse,
			legacy_qty=flt(balance.qty),
			legacy_value=flt(balance.value),
		)
		if stock_engine_adapter.get_valuation_policy(balance.item_code, engine) is None:
			rows.append(row.update({"skipped": True}))
			continue
		state = snapshots.get(
			(balance.item_code, balance.warehouse)
		) or stock_engine_snapshots.get_state_as_of(balance.item_code, balance.warehouse, moment)
		row.update(_get_engine_qty_and_value(engine, state))
		row.delta = flt(row.engine_value - row.legacy_value, 6)
		rows.append(row)
	return rows


def insert_opening_assertions(
	company: str,
	moment: str,
	opening_assertions: Iterable[frappe._dict],
	owner: tuple[str, str] | None = None,
) -> int:
	"""Bulk-insert opening_assertion Assertion facts (``item_code``, ``warehouse``,
	``qty``, ``rate``, ``seeds``) at the moment, optionally owned by a
	voucher whose docstatus governs whether they lock, then drop the
	company's engine state so the next fold starts from them. Returns the
	number of keys pinned."""
	timestamp = frappe.utils.now()
	events: list[list] = []
	allocations: list[list] = []
	keys = 0

	for opening_assertion in opening_assertions:
		event_id = frappe.db.get_next_sequence_val("Stock Event")
		events.append(
			_make_opening_assertion_row(event_id, company, moment, opening_assertion, owner, timestamp)
		)
		for position, seed in enumerate(opening_assertion.seeds or (), start=1):
			allocations.append(_make_allocation_row(event_id, position, seed, timestamp))
		keys += 1
		if len(events) >= FLUSH_AT:
			_flush_opening_assertions(events, allocations)

	_flush_opening_assertions(events, allocations)
	delete_company_engine_state(company)
	return keys


def _make_opening_assertion_row(
	event_id: int, company: str, moment: str, opening_assertion: frappe._dict, owner, timestamp: str
) -> list:
	voucher_type, voucher_no = owner or (None, None)
	return [
		event_id,
		opening_assertion.item_code,
		opening_assertion.warehouse,
		company,
		moment,
		"Assertion",
		0.0,
		0.0,
		flt(opening_assertion.qty),
		flt(opening_assertion.rate),
		0.0,
		voucher_type,
		voucher_no,
		"Baseline",
		timestamp,
		timestamp,
		"Administrator",
		"Administrator",
	]


def _make_allocation_row(event_id: int, position: int, seed: frappe._dict, timestamp: str) -> list:
	return [
		frappe.generate_hash(length=10),
		str(event_id),
		"Stock Event",
		"allocations",
		position,
		seed.get("serial_no"),
		seed.get("batch_no"),
		flt(seed.qty),
		flt(seed.rate),
		timestamp,
		timestamp,
		"Administrator",
		"Administrator",
	]


def _get_engine_qty_and_value(engine, state) -> dict:
	"""What the fold says the key holds: quantity and value (a negative
	balance is exposure, already netted by the engine), the pool rate a
	opening_assertion asserts, and the batch sub-states it seeds. Seeds are dropped
	when the key is negative or the lots overshoot the balance — an assertion
	cannot carry them then."""

	qty, value = flt(state.qty), state.value
	seeds = [
		frappe._dict(batch_no=lot.lot_id, qty=lot.state.qty, rate=lot.state.valuation_rate)
		for lot in state.lots
		if lot.lot_type is engine.LotType.BATCH and lot.state.qty > TOLERANCE
	]
	if qty <= 0 or sum(seed.qty for seed in seeds) > qty + TOLERANCE:
		seeds = []
	rate = flt(state.exposure_rate) if qty < 0 else _get_unlotted_pool_rate(qty, value, seeds)[0]
	return {"engine_qty": qty, "engine_value": value, "rate": rate, "seeds": seeds}


def _get_legacy_closing_balances(company: str, moment: str) -> list[frappe._dict]:
	"""Legacy's stored closing balance per key: the last live SLE at the moment."""
	return frappe.db.sql(
		"""
		SELECT item_code, warehouse, qty_after_transaction AS qty, stock_value AS value
		FROM (
			SELECT sle.item_code, sle.warehouse, sle.qty_after_transaction, sle.stock_value,
				ROW_NUMBER() OVER (
					PARTITION BY sle.item_code, sle.warehouse
					ORDER BY sle.posting_datetime DESC, sle.creation DESC
				) AS recency
			FROM `tabStock Ledger Entry` sle
			INNER JOIN `tabWarehouse` warehouse ON warehouse.name = sle.warehouse
			WHERE sle.is_cancelled = 0
				AND warehouse.company = %(company)s
				AND sle.posting_datetime <= %(moment)s
		) ranked
		WHERE recency = 1
		""",
		{"company": company, "moment": moment},
		as_dict=True,
	)


def _get_lot_seeds_from_legacy(balance: frappe._dict, report: dict) -> list[frappe._dict]:
	"""Lot sub-states to seed from legacy data: batches in valuation only.

	Serials never seed sub-states — they are quantity tags whose rate is
	derived from facts (serial-wise valuation model, §2.6); their quantity
	rides the pool at assert_rate. All-or-nothing per key: if the lots claim
	more quantity than legacy's balance holds (drifted batch data), the whole
	key freezes pool-only."""
	if flt(balance.qty) <= 0 or not _is_lot_tracked(balance.item_code):
		return []

	seeds = []
	for row in _get_live_lots(balance.item_code, balance.warehouse):
		if row.serial_no:
			continue
		if row.batch_no and not _is_batch_in_valuation(row.batch_no):
			continue
		rate = flt(row.in_value) / flt(row.in_qty) if flt(row.in_qty) else 0.0
		seeds.append(frappe._dict(serial_no=row.serial_no, batch_no=row.batch_no, qty=row.qty, rate=rate))

	if sum(flt(seed.qty) for seed in seeds) > flt(balance.qty) + TOLERANCE:
		report["lot_mismatch"] += 1
		return []
	report["lots_seeded"] += len(seeds)
	return seeds


def _get_unlotted_pool_rate(qty: float, value: float, seeds: list[frappe._dict]) -> tuple[float, float]:
	"""(rate of the unlotted pool, value left unassigned when there is no pool)."""
	lot_qty = sum(flt(seed.qty) for seed in seeds)
	lot_value = sum(flt(seed.qty) * flt(seed.rate) for seed in seeds)
	pool_qty = qty - lot_qty
	if pool_qty > TOLERANCE:
		return (value - lot_value) / pool_qty, 0.0
	return (value / qty if qty else 0.0), value - lot_value


def _get_live_lots(item_code: str, warehouse: str) -> list[frappe._dict]:
	return frappe.db.sql(
		"""
		SELECT sbe.serial_no, sbe.batch_no,
			SUM(sbe.qty) AS qty,
			SUM(CASE WHEN sbe.qty > 0 THEN sbe.qty * sbe.incoming_rate ELSE 0 END) AS in_value,
			SUM(CASE WHEN sbe.qty > 0 THEN sbe.qty ELSE 0 END) AS in_qty
		FROM `tabSerial and Batch Entry` sbe
		INNER JOIN `tabSerial and Batch Bundle` bundle ON bundle.name = sbe.parent
		WHERE bundle.item_code = %(item_code)s
			AND sbe.warehouse = %(warehouse)s
			AND bundle.docstatus = 1
			AND bundle.is_cancelled = 0
		GROUP BY sbe.serial_no, sbe.batch_no
		HAVING SUM(sbe.qty) > %(tolerance)s
		""",
		{"item_code": item_code, "warehouse": warehouse, "tolerance": TOLERANCE},
		as_dict=True,
	)


def _is_lot_tracked(item_code: str) -> bool:
	has_batch_no, has_serial_no = frappe.get_cached_value(
		"Item", item_code, ["has_batch_no", "has_serial_no"]
	)
	return bool(has_batch_no or has_serial_no)


def _is_batch_in_valuation(batch_no: str) -> bool:
	return bool(frappe.get_cached_value("Batch", batch_no, "use_batchwise_valuation"))


def _flush_opening_assertions(events: list[list], allocations: list[list]) -> None:
	if events:
		frappe.db.bulk_insert("Stock Event", EVENT_FIELDS, events)
		events.clear()
	if allocations:
		frappe.db.bulk_insert("Stock Event Allocation", stock_event_writer.ALLOCATION_FIELDS, allocations)
		allocations.clear()


def delete_company_engine_state(company: str) -> None:
	"""Drop every engine state of the company; the next fold of each key
	rebuilds from its latest active opening_assertion."""
	warehouses = frappe.get_all("Warehouse", {"company": company}, pluck="name")
	if warehouses:
		frappe.db.delete("Stock Engine State", {"warehouse": ("in", warehouses)})
