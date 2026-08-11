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
COMPANIES_FLAG = "stock_fold_authoritative_companies"
SUPPRESS_FLAG = "stock_fold_suppress_legacy_repost"
APPENDED = "appended"
REFOLDED = "refolded"
REFOLD_CAP = 20000


def try_fold(args: dict, allow_negative_stock: bool = False) -> str | None:
	"""Value this SLE by folding its event.

	Returns APPENDED (event folded onto the checkpoint), REFOLDED (backdated —
	the whole key was refolded and its projections rewritten), or None to fall
	back to the legacy engine.
	"""
	outcome = _try_fold(args, allow_negative_stock)
	_record_outcome(args, outcome)
	return outcome


def should_skip_legacy_repost(doc) -> bool:
	"""True when every SLE of this voucher was fold-valued and the site opted
	out of the legacy background repost — nothing is left for it to do: values
	were written synchronously and refolds regenerate affected GL inline."""
	if not frappe.conf.get(SUPPRESS_FLAG):
		return False

	voucher = (doc.doctype, doc.name)
	return voucher in _outcomes("folded") and voucher not in _outcomes("fallback")


def _record_outcome(args: dict, outcome: str | None) -> None:
	voucher = (args.get("voucher_type"), args.get("voucher_no"))
	_outcomes("fallback" if outcome is None else "folded").add(voucher)


def _outcomes(kind: str) -> set:
	attr = f"stock_fold_{kind}_vouchers"
	if not hasattr(frappe.local, attr):
		setattr(frappe.local, attr, set())
	return getattr(frappe.local, attr)


def _try_fold(args: dict, allow_negative_stock: bool) -> str | None:
	if not _applies(args):
		return None

	from erpnext.stock.services import stock_engine_bridge

	engine = stock_engine_bridge.engine()
	policy = stock_engine_bridge.policy_for(args.get("item_code"), engine)
	if policy is None:
		return None

	event_row = _event_row(args.get("name"))
	if not event_row:
		return None

	if _has_future_events(event_row):
		return _refold(engine, policy, event_row, args, allow_negative_stock)

	state, last_event = _load_state(engine, event_row)
	if state is None:
		return None

	event = stock_engine_bridge.to_event(engine, event_row)
	if event.id <= last_event:
		return None

	result = engine.replay([event], engine.FoldContext(policy=policy), start=state)
	effect = result.effects[event.id]
	_validate_negative(effect, args, allow_negative_stock)

	_project_sle(event_row.sle, result.final, effect, policy, engine)
	_project_bin(event_row, result.final)
	_save_state(engine, event_row, result.final)
	return APPENDED


def _applies(args: dict) -> bool:
	if not (frappe.conf.get(FLAG) and frappe.conf.get("stock_event_dual_write")):
		return False

	companies = frappe.conf.get(COMPANIES_FLAG)
	if companies and args.get("company") not in companies:
		return False

	return not (args.get("serial_and_batch_bundle") or args.get("is_adjustment_entry"))


def _refold(engine, policy, event_row: frappe._dict, args: dict, allow_negative_stock: bool) -> str | None:
	"""Backdated insert: synchronously refold the whole key and rewrite the
	projections of every row whose values changed."""
	from erpnext.stock.services import stock_engine_bridge

	key = {"item_code": event_row.item_code, "warehouse": event_row.warehouse}
	if not _history_foldable(key):
		return None

	rows = frappe.get_all(
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
			"sle",
		],
		order_by="posting_datetime, name",
	)

	try:
		events = [stock_engine_bridge.to_event(engine, row) for row in rows]
	except ValueError:
		return None

	result = engine.replay(events, engine.FoldContext(policy=policy))
	_validate_negative(result.effects[cint(event_row.name)], args, allow_negative_stock)

	live = {
		sle.name: sle
		for sle in frappe.get_all(
			"Stock Ledger Entry",
			filters={**key, "is_cancelled": 0},
			fields=["name", "voucher_type", "voucher_no"],
		)
	}
	for row in rows:
		if row.sle not in live:
			continue
		effect = result.effects[cint(row.name)]
		_project_sle(row.sle, result.states[cint(row.name)], effect, policy, engine)

	_project_bin(event_row, result.final)
	last_id = max(cint(row.name) for row in rows)
	_save_state(engine, frappe._dict({**key, "name": last_id}), result.final)

	if frappe.conf.get(SUPPRESS_FLAG):
		_regenerate_gl(args, event_row, live.values())

	return REFOLDED


def _regenerate_gl(args: dict, event_row: frappe._dict, live_sles) -> None:
	"""With the legacy repost suppressed, correct affected vouchers' GL inline.

	Comparison-based regeneration: only vouchers whose GL no longer matches
	their (refolded) svd get rewritten. The voucher being submitted is
	excluded — its GL posts normally later in the same submit."""
	from erpnext.accounts.utils import repost_gle_for_stock_vouchers

	current = (args.get("voucher_type"), args.get("voucher_no"))
	vouchers = sorted({(sle.voucher_type, sle.voucher_no) for sle in live_sles} - {current})
	repost_gle_for_stock_vouchers(vouchers, str(event_row.posting_datetime)[:10], company=args.get("company"))


def _history_foldable(key: dict) -> bool:
	"""Complete event history, aggregate-only, and small enough for a sync refold."""
	events = frappe.db.count("Stock Event", key)
	if events > REFOLD_CAP:
		return False
	if events < frappe.db.count("Stock Ledger Entry", {**key, "is_cancelled": 0}):
		return False
	return not _key_has_allocations(key)


def _key_has_allocations(key: dict) -> bool:
	event = frappe.qb.DocType("Stock Event")
	allocation = frappe.qb.DocType("Stock Event Allocation")
	rows = (
		frappe.qb.from_(allocation)
		.join(event)
		.on(allocation.parent == event.name)
		.select(allocation.name)
		.where((event.item_code == key["item_code"]) & (event.warehouse == key["warehouse"]))
		.limit(1)
	).run()
	return bool(rows)


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
	if not _history_foldable(key):
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


def _project_sle(sle_name: str, state, effect, policy, engine) -> None:
	"""Write one event's fold result into the legacy SLE projection."""
	from erpnext.stock.services import stock_ledger_writer

	layered = isinstance(policy, engine.Fifo | engine.Lifo)
	stock_queue = [[layer.qty, layer.rate] for layer in state.layers] if layered else []

	stock_ledger_writer.set_fields(
		sle_name,
		{
			"qty_after_transaction": effect.qty_after,
			"valuation_rate": state.valuation_rate,
			"stock_value": effect.value_after,
			"stock_value_difference": effect.value_delta,
			"stock_queue": json.dumps(stock_queue),
		},
	)


def _project_bin(event_row: frappe._dict, final_state) -> None:
	from erpnext.stock.services import bin_writer
	from erpnext.stock.utils import get_or_make_bin

	bin_name = get_or_make_bin(event_row.item_code, event_row.warehouse)
	bin_writer.set_fields(
		bin_name,
		{
			"actual_qty": final_state.qty,
			"stock_value": final_state.value,
			"valuation_rate": final_state.valuation_rate,
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
