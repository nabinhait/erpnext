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
from frappe.utils import cint, flt, nowdate

FLAG = "stock_fold_authoritative"
COMPANIES_FLAG = "stock_fold_authoritative_companies"
SUPPRESS_FLAG = "stock_fold_suppress_legacy_repost"
GL_ADJUSTMENT_FLAG = "stock_fold_gl_adjustment"
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
		return _refold(engine, policy, event_row, args, allow_negative_stock)

	state, last_event = _load_state(engine, event_row)
	if state is None:
		return None

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

	return not args.get("is_adjustment_entry")


def _policy_for(engine, item_code: str):
	"""Item's fold policy; lot-tracked items fold per lot at moving average —
	the semantics of legacy batch-wise valuation and per-serial rates."""
	from erpnext.stock.services import stock_engine_bridge

	base = stock_engine_bridge.policy_for(item_code, engine)
	if base is None:
		return None

	detail = frappe.get_cached_value("Item", item_code, ["has_batch_no", "has_serial_no"], as_dict=1)
	if detail and (detail.has_batch_no or detail.has_serial_no):
		return engine.MovingAverage()
	return base


def _allocations(event_names: list) -> dict[str, list[frappe._dict]]:
	rows = frappe.get_all(
		"Stock Event Allocation",
		filters={"parent": ("in", [str(name) for name in event_names])},
		fields=["parent", "serial_no", "batch_no", "qty_change"],
		order_by="idx",
	)
	grouped: dict[str, list[frappe._dict]] = {}
	for row in rows:
		grouped.setdefault(str(row.parent), []).append(row)
	return grouped


def _refold(engine, policy, event_row: frappe._dict, args: dict, allow_negative_stock: bool) -> str | None:
	"""Backdated insert: synchronously refold the whole key and rewrite the
	projections of every row whose values changed."""
	from erpnext.stock.services import stock_engine_bridge

	key = {"item_code": event_row.item_code, "warehouse": event_row.warehouse}
	if not _history_foldable(key, allow_lots=False):
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

	window = _refold_window(rows, event_row)
	is_tail = window[1] == len(rows)
	rows = rows[window[0] : window[1]]
	# a boundary assertion reconstructs the state but its own stored values are
	# untouched by the backdate — never re-project it
	changed_rows = rows[1:] if window[0] > 0 else rows

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
			filters={**key, "is_cancelled": 0, "name": ("in", [row.sle for row in changed_rows if row.sle])},
			fields=["name", "voucher_type", "voucher_no", "stock_value_difference"],
		)
	}
	for row in changed_rows:
		if row.sle not in live:
			continue
		effect = result.effects[cint(row.name)]
		_project_sle(row.sle, result.states[cint(row.name)], effect, policy, engine)

	if is_tail:
		# the window reaches the present, so latest state and checkpoint move
		_project_bin(event_row, result.final)
		last_id = max(cint(row.name) for row in rows)
		_save_state(engine, frappe._dict({**key, "name": last_id}), result.final)

	if frappe.conf.get(GL_ADJUSTMENT_FLAG):
		_post_gl_adjustment(args, event_row, changed_rows, result, live)
	elif frappe.conf.get(SUPPRESS_FLAG):
		_regenerate_gl(args, event_row, live.values())

	return REFOLDED


def _post_gl_adjustment(args: dict, event_row: frappe._dict, rows: list, result, live: dict) -> None:
	"""Append-only GL: never rewrite affected vouchers' postings.

	The net svd deltas the refold caused are posted as fresh GL rows on the
	triggering (backdated) voucher, dated with it, netted per counter account.
	Historical vouchers keep the GL they were reported with; the correction is
	its own auditable posting."""
	from erpnext.accounts.general_ledger import make_gl_entries
	from erpnext.stock import get_warehouse_account_map

	account_map = get_warehouse_account_map(args.get("company"))
	warehouse_account = (account_map.get(event_row.warehouse) or {}).get("account")
	if not warehouse_account:
		return  # no perpetual stock GL on this warehouse: nothing to correct

	current = (args.get("voucher_type"), args.get("voucher_no"))
	deltas: dict[str, float] = {}
	for row in rows:
		stored = live.get(row.sle)
		if stored is None or (stored.voucher_type, stored.voucher_no) == current:
			continue

		delta = flt(result.effects[cint(row.name)].value_delta) - flt(stored.stock_value_difference)
		if abs(delta) < 0.005:
			continue

		counter = _counter_account(stored.voucher_type, stored.voucher_no, warehouse_account)
		if counter:
			deltas[counter] = deltas.get(counter, 0.0) + delta

	gl_map = []
	for counter, delta in sorted(deltas.items()):
		gl_map.append(_adjustment_row(args, warehouse_account, counter, debit=delta))
		gl_map.append(_adjustment_row(args, counter, warehouse_account, debit=-delta))

	if gl_map:
		make_gl_entries(gl_map)


def _counter_account(voucher_type: str, voucher_no: str, warehouse_account: str) -> str | None:
	against = frappe.db.get_value(
		"GL Entry",
		{
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"account": warehouse_account,
			"is_cancelled": 0,
		},
		"against",
	)
	return against.split(",")[0].strip() if against else None


def _adjustment_row(args: dict, account: str, against: str, debit: float) -> frappe._dict:
	return frappe._dict(
		{
			"account": account,
			"against": against,
			"debit": debit if debit > 0 else 0,
			"credit": -debit if debit < 0 else 0,
			"debit_in_account_currency": debit if debit > 0 else 0,
			"credit_in_account_currency": -debit if debit < 0 else 0,
			"voucher_type": args.get("voucher_type"),
			"voucher_no": args.get("voucher_no"),
			"company": args.get("company"),
			# the correction is a fact of now: it posts in the open period, never
			# into the backdated (possibly closed) one
			"posting_date": nowdate(),
			"cost_center": frappe.get_cached_value("Company", args.get("company"), "cost_center"),
			"remarks": "Stock value adjustment for backdated entry",
			"is_opening": "No",
		}
	)


def _refold_window(rows: list, event_row: frappe._dict) -> tuple[int, int]:
	"""The slice of history a backdate can actually change.

	An assertion pins quantity and value, so the refold starts at the last
	assertion at or before the inserted event (folded from empty, it
	reconstructs the exact state) and stops after the first assertion beyond
	it. A reversal referencing an event before the window forces a full
	refold — its source layer lives outside the slice."""
	inserted = next(index for index, row in enumerate(rows) if cint(row.name) == cint(event_row.name))

	start = 0
	for index in range(inserted, -1, -1):
		if rows[index].kind == "Assertion":
			start = index
			break

	end = len(rows)
	for index in range(inserted + 1, len(rows)):
		if rows[index].kind == "Assertion":
			end = index + 1
			break

	window_start_id = cint(rows[start].name)
	for row in rows[start:end]:
		if row.kind == "Reversal" and row.reverses_event and cint(row.reverses_event) < window_start_id:
			return (0, len(rows))

	return (start, end)


def _regenerate_gl(args: dict, event_row: frappe._dict, live_sles) -> None:
	"""With the legacy repost suppressed, correct affected vouchers' GL inline.

	Comparison-based regeneration: only vouchers whose GL no longer matches
	their (refolded) svd get rewritten. The voucher being submitted is
	excluded — its GL posts normally later in the same submit."""
	from erpnext.accounts.utils import repost_gle_for_stock_vouchers

	current = (args.get("voucher_type"), args.get("voucher_no"))
	vouchers = sorted({(sle.voucher_type, sle.voucher_no) for sle in live_sles} - {current})
	repost_gle_for_stock_vouchers(vouchers, str(event_row.posting_datetime)[:10], company=args.get("company"))


def _history_foldable(key: dict, allow_lots: bool = False) -> bool:
	"""Complete event history, small enough for a sync fold, and — when the key
	is lot-tracked — free of assertions (assertions reset the aggregate but
	cannot reconstruct lots, so lot keys with reconciliations stay legacy)."""
	events = frappe.db.count("Stock Event", key)
	if events > REFOLD_CAP:
		return False
	if events < frappe.db.count("Stock Ledger Entry", {**key, "is_cancelled": 0}):
		return False
	if not _key_has_allocations(key):
		return True
	if not allow_lots:
		return False
	return not frappe.db.exists("Stock Event", {**key, "kind": "Assertion"})


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
	if not _history_foldable(key, allow_lots=True):
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

	allocations = _allocations([row.name for row in rows])
	try:
		events_list = [
			stock_engine_bridge.to_event(engine, row, allocations.get(str(row.name))) for row in rows
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
