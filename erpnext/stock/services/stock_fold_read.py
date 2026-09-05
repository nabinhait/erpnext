# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Fold-based reads: any stock state, computed from facts, bounded by checkpoints.

The v17 read model: reports never consult stored SLE values — they take the
nearest Stock Fold Checkpoint at or before the requested moment and fold the
remaining events. Closings therefore bound every read (and every refold), and
history depth stops mattering.

	state_as_of(item_code, warehouse, as_of)   -> engine State
	ledger_rows(item_code, warehouse, from_dt, to_dt) -> per-event running rows
	create_checkpoints(company, to_date)       -> one checkpoint per active key

Checkpoints are a pure performance artifact — disposable, no locking power
(correctness comes from assertions and baselines). They are cut silently by
the monthly scheduler and on Stock Closing Entry submit; the closing entry
is the lock, the checkpoint is just the cache it leaves behind.
"""

import json

import frappe
from frappe.utils import cint, flt

from erpnext.stock.services import stock_engine_bridge

CHECKPOINT_BATCH = 500


def state_as_of(item_code: str, warehouse: str, as_of: str):
	"""The key's fold state at a moment: nearest checkpoint plus folded tail."""
	engine = stock_engine_bridge.engine()
	start, after = _start_state(engine, item_code, warehouse, as_of)
	events = _events(engine, item_code, warehouse, after=after, upto=as_of)
	result = engine.replay(events, engine.FoldContext(policy=_policy(engine, item_code)), start=start)
	return result.final


def state_before(engine, event_row: frappe._dict):
	"""The key's fold state just before this event in the total order: the
	nearest checkpoint, else the last assertion (which reconstructs the state
	from empty), plus the tail up to, excluding, the event."""
	anchor = (str(event_row.posting_datetime), cint(event_row.name))
	start, after = _start_state(engine, event_row.item_code, event_row.warehouse, anchor[0])
	since = None if start else _last_assertion_key(event_row.item_code, event_row.warehouse, anchor)
	events = [
		event
		for event in _events(
			engine, event_row.item_code, event_row.warehouse, after=after, upto=anchor[0], since=since
		)
		if (str(event.posting_datetime), event.id) < anchor
	]
	policy = _policy(engine, event_row.item_code)
	return engine.replay(events, engine.FoldContext(policy=policy), start=start).final


def _start_state(engine, item_code: str, warehouse: str, as_of: str) -> tuple:
	"""(state, as_of) of the nearest checkpoint, or (None, None)."""
	checkpoint = _nearest_checkpoint(item_code, warehouse, as_of)
	if not checkpoint:
		return None, None
	return stock_engine_bridge.deserialize_state(engine, json.loads(checkpoint.state_json)), checkpoint.as_of


def _last_assertion_key(item_code: str, warehouse: str, anchor: tuple) -> tuple | None:
	from erpnext.stock.services.stock_fold_authority import _drop_revoked_baselines

	rows = _drop_revoked_baselines(
		frappe.get_all(
			"Stock Event",
			filters={
				"item_code": item_code,
				"warehouse": warehouse,
				"kind": "Assertion",
				"posting_datetime": ("<=", anchor[0]),
			},
			fields=["name", "posting_datetime", "kind", "sle", "voucher_type", "voucher_no"],
			order_by="posting_datetime desc, name desc",
			limit=5,
		)
	)
	keys = [(str(row.posting_datetime), cint(row.name)) for row in rows]
	return next((key for key in keys if key < anchor), None)


def ledger_rows(item_code: str, warehouse: str, from_dt: str, to_dt: str) -> list[dict]:
	"""Running per-event rows for a ledger view of the window."""
	engine = stock_engine_bridge.engine()
	opening = state_as_of(item_code, warehouse, from_dt)
	events = _events(engine, item_code, warehouse, after=from_dt, upto=to_dt)
	result = engine.replay(events, engine.FoldContext(policy=_policy(engine, item_code)), start=opening)

	meta = (
		{
			cint(row.name): row
			for row in frappe.get_all(
				"Stock Event",
				filters={"name": ("in", [event.id for event in events])},
				fields=["name", "voucher_type", "voucher_no", "voucher_detail_no", "sle"],
			)
		}
		if events
		else {}
	)

	rows = []
	for event in sorted(events, key=lambda e: e.sort_key):
		effect = result.effects[event.id]
		state = result.states[event.id]
		info = meta.get(event.id) or frappe._dict()
		rows.append(
			{
				"event": event.id,
				"voucher_type": info.get("voucher_type"),
				"voucher_no": info.get("voucher_no"),
				"sle": info.get("sle"),
				"posting_datetime": event.posting_datetime,
				"kind": event.kind.value,
				"qty_change": event.qty_change,
				"qty_after": effect.qty_after,
				"value_after": stock_engine_bridge.equivalent_value(state),
				"value_delta": effect.value_delta,
				"valuation_rate": state.valuation_rate,
			}
		)
	return rows


def create_monthly_fold_checkpoints() -> None:
	"""Scheduled monthly: silent checkpoints at last month's end, per company.

	Naturally idempotent — keys already checkpointed at that moment fold no
	new events and are skipped, so reruns cost nothing."""
	from frappe.utils import add_months, get_last_day, nowdate

	to_date = get_last_day(add_months(nowdate(), -1))
	for company in frappe.get_all("Company", pluck="name"):
		if frappe.db.exists("Stock Event", {"company": company}):
			create_checkpoints(company, to_date)


def create_checkpoints(company: str, to_date, closing_entry: str | None = None) -> int:
	"""One fold checkpoint per key active up to to_date's end.

	Keys with no events since their previous checkpoint are skipped — reads
	fall back to the older checkpoint, so sparse keys cost nothing per period.
	"""
	engine = stock_engine_bridge.engine()
	as_of = stock_engine_bridge.end_of_day(to_date)
	created = 0
	buffer: list[dict] = []

	for key in active_keys(company, as_of):
		checkpoint = _nearest_checkpoint(key.item_code, key.warehouse, as_of)
		after = checkpoint.as_of if checkpoint else None

		events = _events(engine, key.item_code, key.warehouse, after=after, upto=as_of)
		if not events:
			continue

		start = None
		if checkpoint:
			start = stock_engine_bridge.deserialize_state(engine, json.loads(checkpoint.state_json))

		policy = _policy(engine, key.item_code)
		if policy is None:
			continue

		result = engine.replay(events, engine.FoldContext(policy=policy), start=start)
		from erpnext.stock.services.stock_fold_authority import _warn_on_lot_cardinality

		_warn_on_lot_cardinality(key.item_code, key.warehouse, result.final)
		buffer.append(
			{
				"name": frappe.generate_hash(length=10),
				"item_code": key.item_code,
				"warehouse": key.warehouse,
				"as_of": as_of,
				"last_event": max(event.id for event in events),
				"stock_closing_entry": closing_entry,
				"state_json": json.dumps(stock_engine_bridge.serialize_state(result.final)),
			}
		)
		created += 1

		if len(buffer) >= CHECKPOINT_BATCH:
			_flush_checkpoints(buffer)

	_flush_checkpoints(buffer)
	return created


def delete_checkpoints(closing_entry_name: str) -> None:
	frappe.db.delete("Stock Fold Checkpoint", {"stock_closing_entry": closing_entry_name})


def _flush_checkpoints(buffer: list[dict]) -> None:
	if not buffer:
		return
	timestamp = frappe.utils.now()
	fields = (
		"name",
		"item_code",
		"warehouse",
		"as_of",
		"last_event",
		"stock_closing_entry",
		"state_json",
		"creation",
		"modified",
		"owner",
		"modified_by",
	)
	audit = {
		"creation": timestamp,
		"modified": timestamp,
		"owner": "Administrator",
		"modified_by": "Administrator",
	}
	values = [[({**row, **audit}).get(field) for field in fields] for row in buffer]
	frappe.db.bulk_insert("Stock Fold Checkpoint", fields, values)
	buffer.clear()
	if not frappe.in_test:
		frappe.db.commit()


def _nearest_checkpoint(item_code: str, warehouse: str, as_of: str):
	rows = frappe.get_all(
		"Stock Fold Checkpoint",
		filters={"item_code": item_code, "warehouse": warehouse, "as_of": ("<=", as_of)},
		fields=["as_of", "state_json"],
		order_by="as_of desc",
		limit=1,
	)
	return rows[0] if rows else None


def active_keys(company: str, as_of: str | None = None, after: str | None = None) -> list[frappe._dict]:
	"""Distinct (item, warehouse) keys of the company with events in the window."""
	event = frappe.qb.DocType("Stock Event")
	query = (
		frappe.qb.from_(event)
		.select(event.item_code, event.warehouse)
		.distinct()
		.where(event.company == company)
	)
	if as_of:
		query = query.where(event.posting_datetime <= as_of)
	if after:
		query = query.where(event.posting_datetime > after)
	return query.orderby(event.item_code).orderby(event.warehouse).run(as_dict=True)


def checkpoint_states(engine, company: str, as_of: str) -> dict:
	"""Every key's checkpointed state at exactly this instant, keyed by
	(item_code, warehouse) — one query instead of one per key."""
	checkpoint = frappe.qb.DocType("Stock Fold Checkpoint")
	warehouse = frappe.qb.DocType("Warehouse")
	rows = (
		frappe.qb.from_(checkpoint)
		.join(warehouse)
		.on(warehouse.name == checkpoint.warehouse)
		.select(checkpoint.item_code, checkpoint.warehouse, checkpoint.state_json)
		.where((warehouse.company == company) & (checkpoint.as_of == as_of))
	).run(as_dict=True)
	return {
		(row.item_code, row.warehouse): stock_engine_bridge.deserialize_state(
			engine, json.loads(row.state_json)
		)
		for row in rows
	}


def _policy(engine, item_code: str):
	return stock_engine_bridge.policy_for(item_code, engine)


def _events(
	engine, item_code: str, warehouse: str, after=None, upto=None, since: tuple | None = None
) -> list:
	"""Engine events of the key in the window: after (exclusive) .. upto
	(inclusive), or from the sort key ``since`` (inclusive) when given."""
	filters = {"item_code": item_code, "warehouse": warehouse}
	if since:
		after = None
		filters["posting_datetime"] = ("between", [since[0], str(upto)]) if upto else (">=", since[0])
	elif after and upto:
		filters["posting_datetime"] = ("between", [str(after), str(upto)])
	elif after:
		filters["posting_datetime"] = (">", str(after))
	elif upto:
		filters["posting_datetime"] = ("<=", str(upto))

	rows = frappe.get_all(
		"Stock Event",
		filters=filters,
		fields=[
			"name",
			"item_code",
			"posting_datetime",
			"kind",
			"qty_change",
			"declared_rate",
			"assert_qty",
			"assert_rate",
			"reverses_event",
			"value_change",
			"sle",
			"voucher_type",
			"voucher_no",
		],
		order_by="posting_datetime, name",
	)
	if after:
		rows = [row for row in rows if str(row.posting_datetime) > str(after)]
	if since:
		rows = [row for row in rows if (str(row.posting_datetime), cint(row.name)) >= since]

	from erpnext.stock.services.stock_fold_authority import _drop_revoked_baselines

	rows = _drop_revoked_baselines(rows)

	bundle_backed = _bundle_backed({row.sle for row in rows if row.sle})
	allocations = _allocations([row.name for row in rows])
	return [
		stock_engine_bridge.to_event(
			engine,
			row,
			allocations.get(str(row.name)) if row.sle in bundle_backed or _is_baseline(row) else None,
		)
		for row in rows
	]


def _is_baseline(row: frappe._dict) -> bool:
	"""An SLE-less assertion is a cutover baseline; its allocations seed lots."""
	return row.kind == "Assertion" and not row.sle


def _bundle_backed(sle_names: set) -> set:
	if not sle_names:
		return set()
	return set(
		frappe.get_all(
			"Stock Ledger Entry",
			filters={"name": ("in", list(sle_names)), "serial_and_batch_bundle": ("is", "set")},
			pluck="name",
		)
	)


def _allocations(event_names: list) -> dict[str, list[frappe._dict]]:
	if not event_names:
		return {}
	rows = frappe.get_all(
		"Stock Event Allocation",
		filters={"parent": ("in", [str(name) for name in event_names])},
		fields=["parent", "serial_no", "batch_no", "qty_change", "declared_rate"],
		order_by="idx",
	)
	grouped: dict[str, list[frappe._dict]] = {}
	for row in rows:
		grouped.setdefault(str(row.parent), []).append(row)
	return grouped
