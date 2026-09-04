# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Freeze-the-past cutover baseline.

The grandfathering decision made concrete: one SLE-less Assertion fact per
(item, warehouse) key pins legacy's stored balance at the freeze instant.
Refolds and state rebuilds never walk behind a baseline, so historical
values — including legacy's negative-stock math — stay exactly as written,
while forward folding starts clean: a negative balance freezes as modelled
exposure settled by the next receipts, batchwise batches and live serials
are seeded as lot sub-states, and quantity-tag batches ride the pool.

Emits facts only — no SLE rows, no GL, no repricing. Running it again
simply lays a fresh baseline on top; refolds always start from the latest.

    bench --site <site> execute erpnext.stock.services.stock_fold_cutover.freeze_baseline \
        --kwargs "{'company': 'My Company Ltd'}"
"""

import frappe
from frappe.utils import flt

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
ALLOCATION_FIELDS = (
	"name",
	"parent",
	"parenttype",
	"parentfield",
	"idx",
	"serial_no",
	"batch_no",
	"qty_change",
	"declared_rate",
	"creation",
	"modified",
	"owner",
	"modified_by",
)
FLUSH_AT = 500
TOLERANCE = 1e-6


def freeze_baseline(company: str, moment: str | None = None, closing_entry: str | None = None) -> dict:
	"""Emit one baseline Assertion per key of the company, then invalidate
	fold state so the next fold rebuilds from the baseline.

	With closing_entry, the baselines are owned by that Stock Closing Entry:
	they lock only while it stays submitted — cancelling the closing revokes
	them (the frontier model). Without it, the freeze is unconditional."""
	moment = str(moment or frappe.utils.now_datetime())
	timestamp = frappe.utils.now()
	report = {
		"company": company,
		"moment": moment,
		"keys": 0,
		"negative": 0,
		"lots_seeded": 0,
		"lot_mismatch": 0,
		"pool_residual": 0.0,
	}
	events: list[list] = []
	allocations: list[list] = []

	for balance in _closing_balances(company, moment):
		event_id = frappe.db.get_next_sequence_val("Stock Event")
		seeds = _key_seeds(balance, report)
		assert_rate = _pool_rate(balance, seeds, report)
		events.append(
			[
				event_id,
				balance.item_code,
				balance.warehouse,
				company,
				moment,
				"Assertion",
				0.0,
				0.0,
				flt(balance.qty),
				assert_rate,
				0.0,
				"Stock Closing Entry" if closing_entry else None,
				closing_entry,
				"Baseline",
				timestamp,
				timestamp,
				"Administrator",
				"Administrator",
			]
		)
		for position, seed in enumerate(seeds, start=1):
			allocations.append(
				[
					frappe.generate_hash(length=10),
					str(event_id),
					"Stock Event",
					"allocations",
					position,
					seed.serial_no,
					seed.batch_no,
					flt(seed.qty),
					flt(seed.rate),
					timestamp,
					timestamp,
					"Administrator",
					"Administrator",
				]
			)
		report["keys"] += 1
		if flt(balance.qty) < 0:
			report["negative"] += 1
		if len(events) >= FLUSH_AT:
			_flush(events, allocations)

	_flush(events, allocations)
	_invalidate_fold_state(company)
	report["pool_residual"] = flt(report["pool_residual"], 4)
	return report


def _closing_balances(company: str, moment: str) -> list[frappe._dict]:
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


def _key_seeds(balance: frappe._dict, report: dict) -> list[frappe._dict]:
	"""Lot sub-states to seed: live serials, and batches in valuation.

	All-or-nothing per key: if the lots claim more quantity than legacy's
	balance holds (drifted batch data), the whole key freezes pool-only."""
	if flt(balance.qty) <= 0 or not _lot_tracked(balance.item_code):
		return []

	seeds = []
	for row in _live_lots(balance.item_code, balance.warehouse):
		if row.batch_no and not row.serial_no and not _batch_in_valuation(row.batch_no):
			continue
		rate = flt(row.in_value) / flt(row.in_qty) if flt(row.in_qty) else 0.0
		seeds.append(frappe._dict(serial_no=row.serial_no, batch_no=row.batch_no, qty=row.qty, rate=rate))

	if sum(flt(seed.qty) for seed in seeds) > flt(balance.qty) + TOLERANCE:
		report["lot_mismatch"] += 1
		return []
	report["lots_seeded"] += len(seeds)
	return seeds


def _pool_rate(balance: frappe._dict, seeds: list[frappe._dict], report: dict) -> float:
	qty, value = flt(balance.qty), flt(balance.value)
	lot_qty = sum(flt(seed.qty) for seed in seeds)
	lot_value = sum(flt(seed.qty) * flt(seed.rate) for seed in seeds)
	pool_qty = qty - lot_qty
	if pool_qty > TOLERANCE:
		return (value - lot_value) / pool_qty
	report["pool_residual"] += value - lot_value
	return value / qty if qty else 0.0


def _live_lots(item_code: str, warehouse: str) -> list[frappe._dict]:
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


def _lot_tracked(item_code: str) -> bool:
	has_batch_no, has_serial_no = frappe.get_cached_value(
		"Item", item_code, ["has_batch_no", "has_serial_no"]
	)
	return bool(has_batch_no or has_serial_no)


def _batch_in_valuation(batch_no: str) -> bool:
	return bool(frappe.get_cached_value("Batch", batch_no, "use_batchwise_valuation"))


def _flush(events: list[list], allocations: list[list]) -> None:
	if events:
		frappe.db.bulk_insert("Stock Event", EVENT_FIELDS, events)
		events.clear()
	if allocations:
		frappe.db.bulk_insert("Stock Event Allocation", ALLOCATION_FIELDS, allocations)
		allocations.clear()
	if not frappe.in_test:
		frappe.db.commit()


def _invalidate_fold_state(company: str) -> None:
	warehouses = frappe.get_all("Warehouse", {"company": company}, pluck="name")
	if warehouses:
		frappe.db.delete("Stock Fold State", {"warehouse": ("in", warehouses)})
