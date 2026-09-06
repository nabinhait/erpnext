# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Fold-authoritative valuation (Phase 3 cutover, incremental).

With site config ``stock_engine_valuation`` on (requires
``stock_event_dual_write``), the submit hot path values stock by folding the
new Stock Event onto the key's persisted engine state instead of running
``update_entries_after``. The legacy SLE row is still written — its valuation
fields become a projection of the fold's EventEffect, so GL derivation, Bin, and
every report keep working unchanged.

Anything the fold does not yet cover falls back to the legacy engine, per
event: Standard Cost, lot keys with legacy reconciliations, and keys whose
event history is incomplete. Backdates recompute the key synchronously
(``stock_engine_recompute``) or, past SYNC_RECOMPUTE_CAP, value their own row now and
queue the rest. Whenever the legacy engine rewrites a key
(``stock_ledger_writer.update_valuation``), the key's engine state is
invalidated and rebuilt from events on its next fold. Correctness never
depends on the snapshot: it is disposable tier-2 state.
"""

import json

import frappe
from frappe.query_builder.functions import Count
from frappe.utils import cint

FLAG = "stock_engine_valuation"
COMPANIES_FLAG = "stock_engine_valuation_companies"
SUPPRESS_FLAG = "stock_engine_suppress_legacy_repost"
GL_ADJUSTMENT_FLAG = "stock_engine_gl_adjustment"
APPENDED = "appended"
RECOMPUTED = "recomputed"
QUEUED = "queued"
LOT_CARDINALITY_GUARDRAIL = 5000


def is_dual_write_enabled() -> bool:
	"""Whether engine state can exist on this site at all — dual write is the
	master switch, so a legacy rewrite must invalidate even while authority
	is switched off."""
	return bool(frappe.conf.get("stock_event_dual_write"))


def _is_engine_enabled_for_company(company: str | None) -> bool:
	"""Engine valuation is on for the site and, when scoped, for this company."""
	if not (frappe.conf.get(FLAG) and is_dual_write_enabled()):
		return False
	companies = frappe.conf.get(COMPANIES_FLAG)
	return not companies or company in companies


def value_stock_ledger_entry(args: dict, allow_negative_stock: bool = False) -> str | None:
	"""Value this SLE by folding its event.

	Returns APPENDED (event folded onto the snapshot), RECOMPUTED (backdated —
	the whole key was recomputed and its projections rewritten), or None to fall
	back to the legacy engine.
	"""
	outcome = _value_stock_ledger_entry(args, allow_negative_stock)
	_record_valuation_outcome(args, outcome)
	return outcome


def is_voucher_valued_by_engine(doc) -> bool:
	"""True when every SLE of this voucher was fold-valued and the site opted
	out of the legacy background repost — nothing is left for it to do: values
	were written synchronously and recomputes regenerate affected GL inline."""
	if not (frappe.conf.get(SUPPRESS_FLAG) or frappe.conf.get(GL_ADJUSTMENT_FLAG)):
		return False

	voucher = (doc.doctype, doc.name)
	return voucher in _valuation_outcomes("folded") and voucher not in _valuation_outcomes("fallback")


def _record_valuation_outcome(args: dict, outcome: str | None) -> None:
	voucher = (args.get("voucher_type"), args.get("voucher_no"))
	_valuation_outcomes("fallback" if outcome is None else "folded").add(voucher)


def _valuation_outcomes(kind: str) -> set:
	attr = f"stock_engine_{kind}_vouchers"
	if not hasattr(frappe.local, attr):
		setattr(frappe.local, attr, set())
	return getattr(frappe.local, attr)


def _value_stock_ledger_entry(args: dict, allow_negative_stock: bool) -> str | None:
	if not _engine_applies(args):
		return None

	from erpnext.stock.services import stock_engine_adapter

	engine = stock_engine_adapter.get_engine()
	policy = _get_valuation_policy(engine, args.get("item_code"))
	if policy is None:
		return None

	event_row = _get_event_for_sle(args.get("name"))
	if not event_row:
		return None

	if _has_later_events(event_row):
		from erpnext.stock.services import stock_engine_recompute

		return stock_engine_recompute.recompute_after_event(
			engine, policy, event_row, args, allow_negative_stock
		)

	state, last_event, snapshot = _get_stored_or_rebuilt_state(engine, event_row)
	if state is None:
		return None

	allocations = None
	if args.get("serial_and_batch_bundle"):
		allocations = stock_engine_adapter.get_allocations_by_event([event_row.name]).get(str(event_row.name))
	try:
		event = stock_engine_adapter.make_engine_event(engine, event_row, allocations)
	except ValueError:
		return None

	if event.id <= last_event:
		return None

	result = engine.replay([event], engine.EngineContext(policy=policy), start=state)
	effect = result.effects[event.id]
	_validate_negative_stock(effect, args, allow_negative_stock)

	_write_valuation_to_sle(
		event_row.sle, result.final, effect.qty_after, effect.value_after, effect.value_delta, policy, engine
	)
	_update_bin_from_state(event_row.item_code, event_row.warehouse, result.final)
	_save_engine_state(
		engine, event_row.item_code, event_row.warehouse, cint(event_row.name), result.final, snapshot
	)
	return APPENDED


def _engine_applies(args: dict) -> bool:
	return _is_engine_enabled_for_company(args.get("company")) and not args.get("is_adjustment_entry")


def _get_valuation_policy(engine, item_code: str):
	from erpnext.stock.services import stock_engine_adapter

	return stock_engine_adapter.get_valuation_policy(item_code, engine)


def has_complete_event_history(key: dict, allow_lots: bool = False) -> bool:
	"""Complete event history since the last opening_assertion (an SLE-less assertion
	pinning legacy's stored balance — everything behind it is frozen) and,
	when the key is lot-tracked, free of reconciliations after it (a legacy
	reco resets the aggregate but cannot reconstruct lots; a opening_assertion seeds
	them)."""
	since = _after_opening_assertion_filters(key)
	live_rows = frappe.db.count("Stock Ledger Entry", {**key, "is_cancelled": 0, **since})
	if _count_events_with_live_sles(key, get_latest_opening_assertion_datetime(key)) < live_rows:
		return False
	if not _has_bundle_backed_entries(key):
		return True
	if not allow_lots:
		return False
	return not frappe.db.exists("Stock Event", {**key, "kind": "Assertion", "sle": ("is", "set"), **since})


def _count_events_with_live_sles(key: dict, opening_assertion: str | None) -> int:
	"""Events whose ledger row is still live — exactly the rows a fold must
	replay (the backfill never emits for cancelled rows, dual write does)."""
	event = frappe.qb.DocType("Stock Event")
	ledger = frappe.qb.DocType("Stock Ledger Entry")
	query = (
		frappe.qb.from_(event)
		.join(ledger)
		.on(ledger.name == event.sle)
		.select(Count(event.name))
		.where(
			(event.item_code == key["item_code"])
			& (event.warehouse == key["warehouse"])
			& (ledger.is_cancelled == 0)
		)
	)
	if opening_assertion:
		query = query.where(event.posting_datetime > str(opening_assertion))
	return cint(query.run()[0][0])


def can_recompute_synchronously(key: dict) -> bool:
	"""Few enough events since the opening_assertion for a synchronous recompute."""
	from erpnext.stock.services.stock_engine_recompute import SYNC_RECOMPUTE_CAP

	return _count_events(key, _after_opening_assertion_filters(key)) <= SYNC_RECOMPUTE_CAP


def _after_opening_assertion_filters(key: dict) -> dict:
	opening_assertion = get_latest_opening_assertion_datetime(key)
	return {"posting_datetime": (">", str(opening_assertion))} if opening_assertion else {}


def _count_events(key: dict, since: dict) -> int:
	return frappe.db.count("Stock Event", {**key, **since})


def get_latest_opening_assertion_datetime(key: dict) -> str | None:
	"""The newest *active* opening_assertion. A opening_assertion linked to a Stock Closing Entry
	is active only while that closing is submitted — cancelling the closing
	revokes it and the frontier slides back to the previous opening_assertion."""
	rows = frappe.get_all(
		"Stock Event",
		filters={**key, "kind": "Assertion", "sle": ("is", "not set")},
		fields=["posting_datetime", "voucher_type", "voucher_no"],
		order_by="posting_datetime desc, name desc",
	)
	for row in rows:
		if _is_opening_assertion_active(row):
			return row.posting_datetime
	return None


def _is_opening_assertion_active(row: frappe._dict) -> bool:
	"""An owned opening_assertion (Stock Closing Entry or Stock Opening Adjustment)
	locks only while its owner stays submitted; unowned ones always do."""
	if not (row.voucher_type and row.voucher_no):
		return True
	return cint(frappe.db.get_value(row.voucher_type, row.voucher_no, "docstatus")) == 1


def exclude_revoked_opening_assertions(rows: list) -> list:
	"""A revoked opening_assertion must not fold — replaying it would reset the key to
	its stale pinned state. Active opening_assertions and ordinary rows pass through."""
	from erpnext.stock.services.stock_engine_adapter import is_opening_assertion

	return [row for row in rows if not is_opening_assertion(row) or _is_opening_assertion_active(row)]


def _has_bundle_backed_entries(key: dict) -> bool:
	"""Lots are folded as lots only where legacy's bundle engine valued them;
	field-derived lot facts on pre-bundle rows were valued aggregate."""
	return bool(
		frappe.db.exists(
			"Stock Ledger Entry",
			{**key, "is_cancelled": 0, "serial_and_batch_bundle": ("is", "set")},
		)
	)


def apply_cost_revision(
	item_code: str,
	warehouse: str,
	source_event: int,
	value_change: float,
	voucher_type: str,
	voucher_no: str,
	skip_gl_adjustment: bool = False,
) -> str | None:
	"""Apply a cost revision (landed cost) as a Revaluation fact and recompute.

	Returns RECOMPUTED when the fold handled it; None means the caller must run
	the legacy landed-cost machinery instead (lot-tracked key, incomplete
	history, flags off)."""
	source = frappe.db.get_value(
		"Stock Event",
		source_event,
		["name", "item_code", "warehouse", "company", "posting_datetime", "voucher_type", "voucher_no"],
		as_dict=1,
	)
	if not source or source.item_code != item_code or source.warehouse != warehouse:
		return None
	if not _is_engine_enabled_for_company(source.company):
		return None

	from erpnext.stock.services import stock_engine_adapter, stock_event_writer

	engine = stock_engine_adapter.get_engine()
	policy = _get_valuation_policy(engine, item_code)
	if policy is None:
		return None

	emitted = stock_event_writer.insert_revaluation(
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
	from erpnext.stock.services import stock_engine_recompute

	outcome = stock_engine_recompute.recompute_after_event(
		engine, policy, event_row, args, allow_negative_stock=True
	)
	if outcome is None:
		frappe.db.delete("Stock Event", {"name": emitted.name})
		return None

	_record_valuation_outcome({"voucher_type": voucher_type, "voucher_no": voucher_no}, outcome)
	return outcome


def can_apply_cost_revision(item_code: str, warehouse: str, company: str) -> bool:
	"""Whether a cost revision on this key can take the fold path."""
	if not _is_engine_enabled_for_company(company):
		return False
	if not (frappe.conf.get(SUPPRESS_FLAG) or frappe.conf.get(GL_ADJUSTMENT_FLAG)):
		return False

	from erpnext.stock.services import stock_engine_adapter

	engine = stock_engine_adapter.get_engine()
	if _get_valuation_policy(engine, item_code) is None:
		return False
	key = {"item_code": item_code, "warehouse": warehouse}
	return has_complete_event_history(key, allow_lots=True) and can_recompute_synchronously(key)


def make_cost_revision_gl_entries(
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

	from erpnext.stock.services import stock_engine_recompute as recompute

	posting_date = recompute._get_open_period_date(
		posting_date,
		recompute._get_last_period_closing_date(company),
		fallback_date or frappe.utils.nowdate(),
	)
	args = {"voucher_type": voucher_type, "voucher_no": voucher_no, "company": company}
	make_gl_entries(
		recompute.make_adjustment_gl_pair(args, warehouse_account, credit_account, value_change, posting_date)
	)


def delete_engine_state(item_code: str, warehouse: str, from_datetime=None) -> None:
	"""Drop the key's engine state after a legacy rewrite, plus every snapshot
	photographed at or after the rewritten instant (all of them when the
	instant is unknown). Stale photographs must never seed a read; both
	artifacts rebuild lazily from facts."""
	key = {"item_code": item_code, "warehouse": warehouse}
	frappe.db.delete("Stock Engine State", key)
	checkpoint_filters = dict(key)
	if from_datetime:
		checkpoint_filters["as_of"] = (">=", str(from_datetime))
	frappe.db.delete("Stock Engine Snapshot", checkpoint_filters)


def _get_event_for_sle(sle_name: str | None) -> frappe._dict | None:
	if not sle_name:
		return None

	emitted = getattr(frappe.local, "stock_event_last_inserted", None)
	if emitted is not None and emitted.get("sle") == sle_name:
		return emitted

	from erpnext.stock.services.stock_engine_adapter import EVENT_FIELDS

	rows = frappe.get_all("Stock Event", filters={"sle": sle_name}, fields=EVENT_FIELDS, limit=1)
	return rows[0] if rows else None


def _has_later_events(event_row: frappe._dict) -> bool:
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


def _get_stored_or_rebuilt_state(engine, event_row: frappe._dict) -> tuple:
	"""The key's engine state before this event, locked for this transaction."""
	from erpnext.stock.services import stock_engine_adapter

	stored = frappe.db.get_value(
		"Stock Engine State",
		{"item_code": event_row.item_code, "warehouse": event_row.warehouse},
		["name", "state_json", "last_event"],
		as_dict=1,
		for_update=True,
	)
	if stored:
		state = stock_engine_adapter.deserialize_state(engine, json.loads(stored.state_json))
		return state, cint(stored.last_event), stored.name

	state, last_event = _rebuild_state_from_events(engine, event_row)
	return state, last_event, None


def _rebuild_state_from_events(engine, event_row: frappe._dict) -> tuple:
	"""Replay the key's event history since its opening_assertion (excluding the current
	event).

	Only valid when that history is complete — every live SLE since the
	opening_assertion must have an event; otherwise engine valuation must not claim this
	key yet. History behind a opening_assertion is frozen and never replayed.
	"""
	from erpnext.stock.services import stock_engine_adapter
	from erpnext.stock.services.stock_engine_recompute import get_events_after_opening_assertion

	key = {"item_code": event_row.item_code, "warehouse": event_row.warehouse}
	if not (has_complete_event_history(key, allow_lots=True) and can_recompute_synchronously(key)):
		return None, 0

	rows = [
		row
		for row in get_events_after_opening_assertion(key, get_latest_opening_assertion_datetime(key))
		if cint(row.name) != cint(event_row.name)
	]
	try:
		events_list = stock_engine_adapter.make_engine_events(engine, rows)
	except ValueError:
		return None, 0

	policy = _get_valuation_policy(engine, event_row.item_code)
	result = engine.replay(events_list, engine.EngineContext(policy=policy))
	last = cint(rows[-1].name) if rows else 0
	return result.final, last


def _validate_negative_stock(effect, args: dict, allow_negative_stock: bool) -> None:
	if effect.qty_after >= -1e-9 or allow_negative_stock:
		return

	from erpnext.stock.stock_ledger import NegativeStockError, is_negative_stock_allowed

	if is_negative_stock_allowed(item_code=args.get("item_code")):
		return

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


def _write_valuation_to_sle(
	sle_name: str, state, qty_after: float, value: float, value_delta: float, policy, engine
) -> None:
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
			"stock_value_difference": value_delta,
			"stock_queue": json.dumps(stock_queue),
		},
	)


def _update_bin_from_state(item_code: str, warehouse: str, final_state) -> None:
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


def _save_engine_state(
	engine, item_code: str, warehouse: str, last_event: int, state, snapshot: str | None = None
) -> None:
	from erpnext.stock.services import stock_engine_adapter

	_warn_on_lot_cardinality(item_code, warehouse, state)
	payload = {
		"last_event": last_event,
		"state_json": json.dumps(stock_engine_adapter.serialize_state(state)),
	}
	existing = snapshot or frappe.db.get_value(
		"Stock Engine State", {"item_code": item_code, "warehouse": warehouse}, "name"
	)
	if existing:
		frappe.db.set_value("Stock Engine State", existing, payload, update_modified=True)
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
	frappe.db.bulk_insert("Stock Engine State", tuple(row), [list(row.values())])


def _warn_on_lot_cardinality(item_code: str, warehouse: str, state) -> None:
	"""The state blob is rewritten whole on every fold, so cost grows with the
	number of valuation-participating lots. Announce the scale problem before
	it hurts; the designed escape hatch is per-lot state rows (§2.6)."""
	lots = len(state.lots)
	if lots > LOT_CARDINALITY_GUARDRAIL:
		frappe.logger("stock_engine").warning(
			f"{item_code}/{warehouse} folds {lots} lot sub-states "
			f"(guardrail {LOT_CARDINALITY_GUARDRAIL}); state blob rewrites are O(lots) — "
			"consider quantity-tag semantics for this item or per-lot state storage"
		)
