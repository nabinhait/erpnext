# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Fold-authoritative valuation (Phase 3 cutover, incremental).

With site config ``stock_fold_authoritative`` on (requires
``stock_event_dual_write``), the submit hot path values stock by folding the
new Stock Event onto the key's persisted fold state instead of running
``update_entries_after``. The legacy SLE row is still written — its valuation
fields become a projection of the fold's Effect, so GL derivation, Bin, and
every report keep working unchanged.

Anything the fold does not yet cover falls back to the legacy engine, per
event: lot-tracked rows, Standard Cost, reconciliations, landed-cost reposts,
backdated entries (future events exist), and keys whose event history is
incomplete. Whenever the legacy engine rewrites a key
(``stock_ledger_writer.write_valuation``), the key's fold state is
invalidated and rebuilt from events on its next fold. Correctness never
depends on the checkpoint: it is disposable tier-2 state.
"""

import json

import frappe
from frappe.utils import cint, flt

FLAG = "stock_fold_authoritative"


def try_fold(args: dict, allow_negative_stock: bool = False) -> bool:
	"""Value this SLE by folding its event. Returns False to fall back to legacy."""
	if not (frappe.conf.get(FLAG) and frappe.conf.get("stock_event_dual_write")):
		return False

	if args.get("serial_and_batch_bundle") or args.get("voucher_type") == "Stock Reconciliation":
		return False

	from erpnext.stock.services import stock_engine_bridge

	engine = stock_engine_bridge.engine()
	policy = stock_engine_bridge.policy_for(args.get("item_code"), engine)
	if policy is None:
		return False

	event_row = _event_row(args.get("name"))
	if not event_row or _has_future_events(event_row):
		return False

	state, last_event = _load_state(engine, event_row)
	if state is None:
		return False

	event = stock_engine_bridge.to_event(engine, event_row)
	if event.id <= last_event:
		return False

	result = engine.replay([event], engine.FoldContext(policy=policy), start=state)
	effect = result.effects[event.id]
	_validate_negative(effect, args, allow_negative_stock)

	_project(event_row, result.final, effect, policy, engine)
	_save_state(engine, event_row, result.final)
	return True


def invalidate(item_code: str, warehouse: str) -> None:
	"""Drop the key's checkpoint after a legacy rewrite; next fold rebuilds it."""
	frappe.db.delete("Stock Fold State", {"item_code": item_code, "warehouse": warehouse})


def _event_row(sle_name: str | None) -> frappe._dict | None:
	if not sle_name:
		return None
	rows = frappe.get_all(
		"Stock Event",
		filters={"sle": sle_name},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"posting_datetime",
			"kind",
			"qty_change",
			"declared_rate",
			"assert_qty",
			"assert_rate",
			"reverses_event",
			"sle",
		],
		limit=1,
	)
	return rows[0] if rows else None


def _has_future_events(event_row: frappe._dict) -> bool:
	table = frappe.qb.DocType("Stock Event")
	rows = (
		frappe.qb.from_(table)
		.select(table.name)
		.where(
			(table.item_code == event_row.item_code)
			& (table.warehouse == event_row.warehouse)
			& (
				(table.posting_datetime > event_row.posting_datetime)
				| ((table.posting_datetime == event_row.posting_datetime) & (table.name > event_row.name))
			)
		)
		.limit(1)
	).run()
	return bool(rows)


def _load_state(engine, event_row: frappe._dict) -> tuple:
	"""The key's fold state before this event, locked for this transaction."""
	from erpnext.stock.services import stock_engine_bridge

	stored = frappe.db.get_value(
		"Stock Fold State",
		{"item_code": event_row.item_code, "warehouse": event_row.warehouse},
		["name", "state_json", "last_event"],
		as_dict=1,
		for_update=True,
	)
	if stored:
		return stock_engine_bridge.deserialize_state(engine, json.loads(stored.state_json)), cint(
			stored.last_event
		)

	return _rebuild(engine, event_row)


def _rebuild(engine, event_row: frappe._dict) -> tuple:
	"""Replay the key's full event history (excluding the current event).

	Only valid when the history is complete — every live SLE of the key must
	have an event; otherwise fold authority must not claim this key yet.
	"""
	from erpnext.stock.services import stock_engine_bridge

	key = {"item_code": event_row.item_code, "warehouse": event_row.warehouse}
	live_sles = frappe.db.count("Stock Ledger Entry", {**key, "is_cancelled": 0})
	events = frappe.db.count("Stock Event", key)
	if events < live_sles:
		return None, 0

	rows = [
		row
		for row in frappe.get_all(
			"Stock Event",
			filters=key,
			fields=[
				"name",
				"posting_datetime",
				"kind",
				"qty_change",
				"declared_rate",
				"assert_qty",
				"assert_rate",
				"reverses_event",
			],
			order_by="posting_datetime, name",
		)
		if cint(row.name) != cint(event_row.name)
	]

	try:
		events_list = [stock_engine_bridge.to_event(engine, row) for row in rows]
	except ValueError:
		return None, 0

	policy = stock_engine_bridge.policy_for(event_row.item_code, engine)
	result = engine.replay(events_list, engine.FoldContext(policy=policy))
	last = cint(rows[-1].name) if rows else 0
	return result.final, last


def _validate_negative(effect, args: dict, allow_negative_stock: bool) -> None:
	if effect.qty_after >= -1e-9 or allow_negative_stock:
		return
	if cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock")):
		return

	from erpnext.stock.stock_ledger import NegativeStockError

	frappe.throw(
		frappe._(
			"{0} units of {1} needed in {2} to complete this transaction (projected balance {3})."
		).format(
			abs(effect.qty_after),
			args.get("item_code"),
			args.get("warehouse"),
			effect.qty_after,
		),
		NegativeStockError,
	)


def _project(event_row: frappe._dict, state, effect, policy, engine) -> None:
	"""Write the fold result into the legacy projections: SLE fields and Bin."""
	from erpnext.stock.services import bin_writer, stock_ledger_writer
	from erpnext.stock.utils import get_or_make_bin

	layered = isinstance(policy, engine.Fifo | engine.Lifo)
	stock_queue = [[layer.qty, layer.rate] for layer in state.layers] if layered else []

	stock_ledger_writer.set_fields(
		event_row.sle,
		{
			"qty_after_transaction": effect.qty_after,
			"valuation_rate": state.valuation_rate,
			"stock_value": effect.value_after,
			"stock_value_difference": effect.value_delta,
			"stock_queue": json.dumps(stock_queue),
		},
	)

	bin_name = get_or_make_bin(event_row.item_code, event_row.warehouse)
	bin_writer.set_fields(
		bin_name,
		{
			"actual_qty": effect.qty_after,
			"stock_value": effect.value_after,
			"valuation_rate": state.valuation_rate,
		},
	)


def _save_state(engine, event_row: frappe._dict, state) -> None:
	from erpnext.stock.services import stock_engine_bridge

	payload = {
		"last_event": cint(event_row.name),
		"state_json": json.dumps(stock_engine_bridge.serialize_state(state)),
	}
	existing = frappe.db.get_value(
		"Stock Fold State", {"item_code": event_row.item_code, "warehouse": event_row.warehouse}, "name"
	)
	if existing:
		frappe.db.set_value("Stock Fold State", existing, payload, update_modified=True)
		return

	doc = frappe.get_doc(
		doctype="Stock Fold State",
		item_code=event_row.item_code,
		warehouse=event_row.warehouse,
		**payload,
	)
	doc.flags.ignore_permissions = True
	doc.insert()
