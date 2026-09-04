# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Fold-based reads: any stock state, computed from facts, bounded by checkpoints.

The v17 read model: reports never consult stored SLE values — they take the
nearest Stock Fold Checkpoint at or before the requested moment and fold the
remaining events. Closings therefore bound every read (and every refold), and
history depth stops mattering.

	state_as_of(item_code, warehouse, as_of)   -> engine State
	ledger_rows(item_code, warehouse, from_dt, to_dt) -> per-event running rows
	create_checkpoints(closing_entry_doc)      -> one checkpoint per active key
"""

import json

import frappe
from frappe.utils import cint, flt

from erpnext.stock.services import stock_engine_bridge

CHECKPOINT_BATCH = 500


def state_as_of(item_code: str, warehouse: str, as_of: str):
	"""The key's fold state at a moment: nearest checkpoint plus folded tail."""
	engine = stock_engine_bridge.engine()
	checkpoint = _nearest_checkpoint(item_code, warehouse, as_of)

	start, after = None, None
	if checkpoint:
		start = stock_engine_bridge.deserialize_state(engine, json.loads(checkpoint.state_json))
		after = checkpoint.as_of

	events = _events(engine, item_code, warehouse, after=after, upto=as_of)
	result = engine.replay(events, engine.FoldContext(policy=_policy(engine, item_code)), start=start)
	return result.final


def ledger_rows(item_code: str, warehouse: str, from_dt: str, to_dt: str) -> list[dict]:
	"""Running per-event rows for a ledger view of the window."""
	engine = stock_engine_bridge.engine()
	opening = state_as_of(item_code, warehouse, from_dt)
	events = _events(engine, item_code, warehouse, after=from_dt, upto=to_dt)
	result = engine.replay(events, engine.FoldContext(policy=_policy(engine, item_code)), start=opening)

	meta = {
		cint(row.name): row
		for row in frappe.get_all(
			"Stock Event",
			filters={"name": ("in", [event.id for event in events])},
			fields=["name", "voucher_type", "voucher_no", "voucher_detail_no", "sle"],
		)
	} if events else {}

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
				"value_after": state.value - state.exposure_qty * state.exposure_rate,
				"value_delta": effect.value_delta,
				"valuation_rate": state.valuation_rate,
			}
		)
	return rows


def create_checkpoints(closing_entry) -> int:
	"""One fold checkpoint per key active up to the closing's to_date.

	Keys with no events since their previous checkpoint are skipped — reads
	fall back to the older checkpoint, so sparse keys cost nothing per period.
	"""
	engine = stock_engine_bridge.engine()
	as_of = str(closing_entry.to_date) + " 23:59:59.999999"
	created = 0
	buffer: list[dict] = []

	for index, key in enumerate(_active_keys(closing_entry.company, as_of)):
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
		buffer.append(
			{
				"name": frappe.generate_hash(length=10),
				"item_code": key.item_code,
				"warehouse": key.warehouse,
				"as_of": as_of,
				"last_event": max(event.id for event in events),
				"stock_closing_entry": closing_entry.name,
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
	audit = {"creation": timestamp, "modified": timestamp, "owner": "Administrator", "modified_by": "Administrator"}
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


def _active_keys(company: str, as_of: str) -> list[frappe._dict]:
	event = frappe.qb.DocType("Stock Event")
	return (
		frappe.qb.from_(event)
		.select(event.item_code, event.warehouse)
		.distinct()
		.where((event.company == company) & (event.posting_datetime <= as_of))
		.orderby(event.item_code)
		.orderby(event.warehouse)
	).run(as_dict=True)


def _policy(engine, item_code: str):
	return stock_engine_bridge.policy_for(item_code, engine)


def _events(engine, item_code: str, warehouse: str, after=None, upto=None) -> list:
	filters = {"item_code": item_code, "warehouse": warehouse}
	if after and upto:
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
			"posting_datetime",
			"kind",
			"qty_change",
			"declared_rate",
			"assert_qty",
			"assert_rate",
			"reverses_event",
			"value_change",
			"sle",
		],
		order_by="posting_datetime, name",
	)
	if after:
		rows = [row for row in rows if str(row.posting_datetime) > str(after)]

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
