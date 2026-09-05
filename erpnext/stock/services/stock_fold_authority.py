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
event: Standard Cost, lot keys with legacy reconciliations, and keys whose
event history is incomplete. Backdates refold the key synchronously
(``stock_fold_refold``) or, past REFOLD_CAP, value their own row now and
queue the rest. Whenever the legacy engine rewrites a key
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
GL_ADJUSTMENT_FLAG = "stock_fold_gl_adjustment"
APPENDED = "appended"
REFOLDED = "refolded"
QUEUED = "queued"
LOT_CARDINALITY_GUARDRAIL = 5000


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
	if not (frappe.conf.get(SUPPRESS_FLAG) or frappe.conf.get(GL_ADJUSTMENT_FLAG)):
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
	policy = _policy_for(engine, args.get("item_code"))
	if policy is None:
		return None

	event_row = _event_row(args.get("name"))
	if not event_row:
		return None

	if _has_future_events(event_row):
		from erpnext.stock.services import stock_fold_refold

		return stock_fold_refold.refold_for_event(engine, policy, event_row, args, allow_negative_stock)

	state, last_event, checkpoint = _load_state(engine, event_row)
	if state is None:
		return None

	allocations = None
	if args.get("serial_and_batch_bundle"):
		allocations = _allocations([event_row.name]).get(str(event_row.name))
	try:
		event = stock_engine_bridge.to_event(engine, event_row, allocations)
	except ValueError:
		return None

	if event.id <= last_event:
		return None

	result = engine.replay([event], engine.FoldContext(policy=policy), start=state)
	effect = result.effects[event.id]
	_validate_negative(effect, args, allow_negative_stock)

	_project_sle(
		event_row.sle, result.final, effect.qty_after, effect.value_after, effect.value_delta, policy, engine
	)
	_project_bin(event_row.item_code, event_row.warehouse, result.final)
	_save_state(
		engine, event_row.item_code, event_row.warehouse, cint(event_row.name), result.final, checkpoint
	)
	return APPENDED


def _applies(args: dict) -> bool:
	if not (frappe.conf.get(FLAG) and frappe.conf.get("stock_event_dual_write")):
		return False

	companies = frappe.conf.get(COMPANIES_FLAG)
	if companies and args.get("company") not in companies:
		return False

	return not args.get("is_adjustment_entry")


def _policy_for(engine, item_code: str):
	from erpnext.stock.services import stock_engine_bridge

	return stock_engine_bridge.policy_for(item_code, engine)


def _allocations(event_names: list) -> dict[str, list[frappe._dict]]:
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


def _history_foldable(key: dict, allow_lots: bool = False) -> bool:
	"""Complete event history since the last baseline (an SLE-less assertion
	pinning legacy's stored balance — everything behind it is frozen) and,
	when the key is lot-tracked, free of reconciliations after it (a legacy
	reco resets the aggregate but cannot reconstruct lots; a baseline seeds
	them)."""
	since = _since_baseline(key)
	if _events_since(key, since) < frappe.db.count("Stock Ledger Entry", {**key, "is_cancelled": 0, **since}):
		return False
	if not _key_has_bundles(key):
		return True
	if not allow_lots:
		return False
	return not frappe.db.exists("Stock Event", {**key, "kind": "Assertion", "sle": ("is", "set"), **since})


def within_refold_cap(key: dict) -> bool:
	"""Few enough events since the baseline for a synchronous refold."""
	from erpnext.stock.services.stock_fold_refold import REFOLD_CAP

	return _events_since(key, _since_baseline(key)) <= REFOLD_CAP


def _since_baseline(key: dict) -> dict:
	baseline = _latest_baseline(key)
	return {"posting_datetime": (">", str(baseline))} if baseline else {}


def _events_since(key: dict, since: dict) -> int:
	return frappe.db.count("Stock Event", {**key, **since})


def _latest_baseline(key: dict) -> str | None:
	"""The newest *active* baseline. A baseline linked to a Stock Closing Entry
	is active only while that closing is submitted — cancelling the closing
	revokes it and the frontier slides back to the previous baseline."""
	rows = frappe.get_all(
		"Stock Event",
		filters={**key, "kind": "Assertion", "sle": ("is", "not set")},
		fields=["posting_datetime", "voucher_type", "voucher_no"],
		order_by="posting_datetime desc, name desc",
	)
	for row in rows:
		if _baseline_active(row):
			return row.posting_datetime
	return None


def _baseline_active(row: frappe._dict) -> bool:
	"""An owned baseline (Stock Closing Entry or Stock Opening Adjustment)
	locks only while its owner stays submitted; unowned ones always do."""
	if not (row.voucher_type and row.voucher_no):
		return True
	return cint(frappe.db.get_value(row.voucher_type, row.voucher_no, "docstatus")) == 1


def _is_baseline(row: frappe._dict) -> bool:
	return row.kind == "Assertion" and not row.sle


def _drop_revoked_baselines(rows: list) -> list:
	"""A revoked baseline must not fold — replaying it would reset the key to
	its stale pinned state. Active baselines and ordinary rows pass through."""
	return [row for row in rows if not _is_baseline(row) or _baseline_active(row)]


def _bundle_backed_sles(key: dict) -> set[str]:
	return set(
		frappe.get_all(
			"Stock Ledger Entry",
			filters={**key, "is_cancelled": 0, "serial_and_batch_bundle": ("is", "set")},
			pluck="name",
		)
	)


def _key_has_bundles(key: dict) -> bool:
	"""Lots are folded as lots only where legacy's bundle engine valued them;
	field-derived lot facts on pre-bundle rows were valued aggregate."""
	return bool(
		frappe.db.exists(
			"Stock Ledger Entry",
			{**key, "is_cancelled": 0, "serial_and_batch_bundle": ("is", "set")},
		)
	)


def revalue(
	item_code: str,
	warehouse: str,
	source_event: int,
	value_change: float,
	voucher_type: str,
	voucher_no: str,
	skip_gl_adjustment: bool = False,
) -> str | None:
	"""Apply a cost revision (landed cost) as a Revaluation fact and refold.

	Returns REFOLDED when the fold handled it; None means the caller must run
	the legacy landed-cost machinery instead (lot-tracked key, incomplete
	history, flags off)."""
	if not (frappe.conf.get(FLAG) and frappe.conf.get("stock_event_dual_write")):
		return None

	from erpnext.stock.services import stock_engine_bridge, stock_event_emitter

	engine = stock_engine_bridge.engine()
	policy = _policy_for(engine, item_code)
	if policy is None:
		return None

	source = frappe.db.get_value(
		"Stock Event",
		source_event,
		["name", "item_code", "warehouse", "company", "posting_datetime", "voucher_type", "voucher_no"],
		as_dict=1,
	)
	if not source or source.item_code != item_code or source.warehouse != warehouse:
		return None

	emitted = stock_event_emitter.emit_revaluation(
		item_code,
		warehouse,
		source.company,
		source.posting_datetime,
		source_event,
		value_change,
		voucher_type,
		voucher_no,
	)
	event_row = frappe._dict(
		name=emitted.name,
		item_code=item_code,
		warehouse=warehouse,
		posting_datetime=emitted.posting_datetime,
	)
	# downstream adjustments are carried on the revising voucher; the source
	# receipt's own correction is the caller's (it carries the expense account)
	args = {
		"company": source.company,
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"posting_date": frappe.db.get_value(voucher_type, voucher_no, "posting_date"),
		"exclude_voucher": (source.voucher_type, source.voucher_no),
		"adjustment_remark": "Stock value adjustment for landed cost",
		"skip_gl_adjustment": skip_gl_adjustment,
	}
	from erpnext.stock.services import stock_fold_refold

	outcome = stock_fold_refold.refold_for_event(engine, policy, event_row, args, allow_negative_stock=True)
	if outcome is None:
		frappe.db.delete("Stock Event", {"name": emitted.name})
		return None

	_record_outcome({"voucher_type": voucher_type, "voucher_no": voucher_no}, outcome)
	return outcome


def can_revalue(item_code: str, warehouse: str) -> bool:
	"""Whether a cost revision on this key can take the fold path."""
	if not (frappe.conf.get(FLAG) and frappe.conf.get("stock_event_dual_write")):
		return False
	if not (frappe.conf.get(SUPPRESS_FLAG) or frappe.conf.get(GL_ADJUSTMENT_FLAG)):
		return False

	from erpnext.stock.services import stock_engine_bridge

	engine = stock_engine_bridge.engine()
	if _policy_for(engine, item_code) is None:
		return False
	key = {"item_code": item_code, "warehouse": warehouse}
	return _history_foldable(key, allow_lots=True) and within_refold_cap(key)


def post_revaluation_gl(
	company: str,
	warehouse: str,
	value_change: float,
	posting_date: str,
	voucher_type: str,
	voucher_no: str,
	credit_account: str,
	fallback_date: str | None = None,
) -> None:
	"""The source-side GL of a revaluation: stock up, expense account down,
	carried on the revising voucher, dated at the revalued receipt — clamped
	to the revising voucher's date when the receipt sits in a closed period."""
	from erpnext.accounts.general_ledger import make_gl_entries
	from erpnext.stock import get_warehouse_account_map

	warehouse_account = (get_warehouse_account_map(company).get(warehouse) or {}).get("account")
	if not warehouse_account:
		return

	from erpnext.stock.services import stock_fold_refold as refold

	posting_date = refold._open_period_date(
		posting_date, refold._closed_until(company), fallback_date or frappe.utils.nowdate()
	)
	args = {"voucher_type": voucher_type, "voucher_no": voucher_no, "company": company}
	make_gl_entries(
		refold.adjustment_pair(args, warehouse_account, credit_account, value_change, posting_date)
	)


def invalidate(item_code: str, warehouse: str, from_datetime=None) -> None:
	"""Drop the key's fold state after a legacy rewrite, plus every checkpoint
	photographed at or after the rewritten instant (all of them when the
	instant is unknown). Stale photographs must never seed a read; both
	artifacts rebuild lazily from facts."""
	key = {"item_code": item_code, "warehouse": warehouse}
	frappe.db.delete("Stock Fold State", key)
	checkpoint_filters = dict(key)
	if from_datetime:
		checkpoint_filters["as_of"] = (">=", str(from_datetime))
	frappe.db.delete("Stock Fold Checkpoint", checkpoint_filters)


def _event_row(sle_name: str | None) -> frappe._dict | None:
	if not sle_name:
		return None

	emitted = getattr(frappe.local, "stock_event_last_emitted", None)
	if emitted is not None and emitted.get("sle") == sle_name:
		return emitted

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
			"value_change",
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
		state = stock_engine_bridge.deserialize_state(engine, json.loads(stored.state_json))
		return state, cint(stored.last_event), stored.name

	state, last_event = _rebuild(engine, event_row)
	return state, last_event, None


def _rebuild(engine, event_row: frappe._dict) -> tuple:
	"""Replay the key's event history since its baseline (excluding the current
	event).

	Only valid when that history is complete — every live SLE since the
	baseline must have an event; otherwise fold authority must not claim this
	key yet. History behind a baseline is frozen and never replayed.
	"""
	from erpnext.stock.services import stock_engine_bridge
	from erpnext.stock.services.stock_fold_refold import _rows_since

	key = {"item_code": event_row.item_code, "warehouse": event_row.warehouse}
	if not (_history_foldable(key, allow_lots=True) and within_refold_cap(key)):
		return None, 0

	rows = [row for row in _rows_since(key, _latest_baseline(key)) if cint(row.name) != cint(event_row.name)]

	bundle_rows = _bundle_backed_sles(key)
	allocations = _allocations([row.name for row in rows])
	try:
		events_list = [
			stock_engine_bridge.to_event(
				engine,
				row,
				allocations.get(str(row.name)) if row.sle in bundle_rows or _is_baseline(row) else None,
			)
			for row in rows
		]
	except ValueError:
		return None, 0

	policy = _policy_for(engine, event_row.item_code)
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


def _project_sle(sle_name: str, state, qty_after: float, value: float, svd: float, policy, engine) -> None:
	"""Write a fold result into the legacy SLE projection."""
	from erpnext.stock.services import stock_ledger_writer

	layered = isinstance(policy, engine.Fifo | engine.Lifo)
	stock_queue = [[layer.qty, layer.rate] for layer in state.layers] if layered else []

	stock_ledger_writer.set_fields(
		sle_name,
		{
			"qty_after_transaction": qty_after,
			"valuation_rate": state.valuation_rate,
			"stock_value": value,
			"stock_value_difference": svd,
			"stock_queue": json.dumps(stock_queue),
		},
	)


def _project_bin(item_code: str, warehouse: str, final_state) -> None:
	from erpnext.stock.services import bin_writer
	from erpnext.stock.utils import get_or_make_bin

	bin_name = get_or_make_bin(item_code, warehouse)
	bin_writer.set_fields(
		bin_name,
		{
			"actual_qty": final_state.qty,
			"stock_value": final_state.value,
			"valuation_rate": final_state.valuation_rate,
		},
	)


def _save_state(
	engine, item_code: str, warehouse: str, last_event: int, state, checkpoint: str | None = None
) -> None:
	from erpnext.stock.services import stock_engine_bridge

	_warn_on_lot_cardinality(item_code, warehouse, state)
	payload = {
		"last_event": last_event,
		"state_json": json.dumps(stock_engine_bridge.serialize_state(state)),
	}
	existing = checkpoint or frappe.db.get_value(
		"Stock Fold State", {"item_code": item_code, "warehouse": warehouse}, "name"
	)
	if existing:
		frappe.db.set_value("Stock Fold State", existing, payload, update_modified=True)
		return

	timestamp = frappe.utils.now()
	row = {
		"name": frappe.generate_hash(length=10),
		"item_code": item_code,
		"warehouse": warehouse,
		**payload,
		"creation": timestamp,
		"modified": timestamp,
		"owner": "Administrator",
		"modified_by": "Administrator",
	}
	frappe.db.bulk_insert("Stock Fold State", tuple(row), [list(row.values())])


def _warn_on_lot_cardinality(item_code: str, warehouse: str, state) -> None:
	"""The state blob is rewritten whole on every fold, so cost grows with the
	number of valuation-participating lots. Announce the scale problem before
	it hurts; the designed escape hatch is per-lot state rows (§2.6)."""
	lots = len(state.lots)
	if lots > LOT_CARDINALITY_GUARDRAIL:
		frappe.logger("stock_fold").warning(
			f"{item_code}/{warehouse} folds {lots} lot sub-states "
			f"(guardrail {LOT_CARDINALITY_GUARDRAIL}); state blob rewrites are O(lots) — "
			"consider quantity-tag semantics for this item or per-lot state storage"
		)
