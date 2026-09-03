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

	_project_sle(event_row.sle, result.final, effect, policy, engine)
	_project_bin(event_row, result.final)
	_save_state(engine, event_row, result.final, checkpoint)
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
		fields=["parent", "serial_no", "batch_no", "qty_change"],
		order_by="idx",
	)
	grouped: dict[str, list[frappe._dict]] = {}
	for row in rows:
		grouped.setdefault(str(row.parent), []).append(row)
	return grouped


def _refold(engine, policy, event_row: frappe._dict, args: dict, allow_negative_stock: bool) -> str | None:
	"""Backdated insert or revaluation: synchronously refold the whole key and
	rewrite the projections of every row whose values changed. Bundle-backed
	lot keys fold their allocations as lot sub-states; lot keys carrying
	assertions stay legacy (an assertion cannot reconstruct lots)."""
	from erpnext.stock.services import stock_engine_bridge

	key = {"item_code": event_row.item_code, "warehouse": event_row.warehouse}
	if not _history_foldable(key, allow_lots=True):
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
			"value_change",
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

	bundle_rows = _bundle_backed_sles(key)
	allocations = _allocations([row.name for row in rows])
	try:
		events = [
			stock_engine_bridge.to_event(
				engine, row, allocations.get(str(row.name)) if row.sle in bundle_rows else None
			)
			for row in rows
		]
	except ValueError:
		return None

	result = engine.replay(events, engine.FoldContext(policy=policy))
	_validate_negative(result.effects[cint(event_row.name)], args, allow_negative_stock)

	live = {
		sle.name: sle
		for sle in frappe.get_all(
			"Stock Ledger Entry",
			filters={**key, "is_cancelled": 0, "name": ("in", [row.sle for row in changed_rows if row.sle])},
			fields=["name", "voucher_type", "voucher_no", "posting_date", "stock_value_difference"],
		)
	}
	start_value = 0.0
	if window[0] > 0:
		start_value = _equivalent_value(result.states[cint(rows[0].name)])
	projections = _absorbed_projections(changed_rows, result, start_value)
	for sle_name, projection in projections.items():
		if sle_name not in live:
			continue
		_project_sle_values(sle_name, projection, policy, engine)

	if is_tail:
		# the window reaches the present, so latest state and checkpoint move
		_project_bin(event_row, result.final)
		last_id = max(cint(row.name) for row in rows)
		_save_state(engine, frappe._dict({**key, "name": last_id}), result.final)

	if frappe.conf.get(GL_ADJUSTMENT_FLAG):
		_post_gl_adjustment(args, event_row, projections, live)
	elif frappe.conf.get(SUPPRESS_FLAG):
		_regenerate_gl(args, event_row, live.values())

	return REFOLDED


def _equivalent_value(state) -> float:
	return state.value - state.exposure_qty * state.exposure_rate


def _absorbed_projections(rows: list, result, start_value: float) -> dict:
	"""Per-SLE projection values with SLE-less events (revaluations) absorbed
	into the preceding SLE row — the shape legacy books carry landed cost in."""
	projections: dict[str, dict] = {}
	prev_value = start_value
	index, total = 0, len(rows)

	while index < total and not rows[index].sle:
		prev_value = _equivalent_value(result.states[cint(rows[index].name)])
		index += 1

	while index < total:
		row = rows[index]
		tail = index
		while tail + 1 < total and not rows[tail + 1].sle:
			tail += 1
		state = result.states[cint(rows[tail].name)]
		value = _equivalent_value(state)
		projections[row.sle] = {
			"qty_after": result.effects[cint(row.name)].qty_after,
			"value": value,
			"svd": value - prev_value,
			"state": state,
		}
		prev_value = value
		index = tail + 1

	return projections


def _project_sle_values(sle_name: str, projection: dict, policy, engine) -> None:
	from erpnext.stock.services import stock_ledger_writer

	state = projection["state"]
	layered = isinstance(policy, engine.Fifo | engine.Lifo)
	stock_queue = [[layer.qty, layer.rate] for layer in state.layers] if layered else []
	stock_ledger_writer.set_fields(
		sle_name,
		{
			"qty_after_transaction": projection["qty_after"],
			"valuation_rate": state.valuation_rate,
			"stock_value": projection["value"],
			"stock_value_difference": projection["svd"],
			"stock_queue": json.dumps(stock_queue),
		},
	)


def _post_gl_adjustment(args: dict, event_row: frappe._dict, projections: dict, live: dict) -> None:
	"""Append-only GL: never rewrite affected vouchers' postings.

	The net svd deltas the refold caused are posted as fresh GL rows on the
	triggering voucher, netted per counter account and dated on the affected
	voucher's own posting date — every correction takes effect exactly when
	the movement it corrects took effect, so stock value and stock account
	balance agree on every as-of date. Closings guarantee those dates lie in
	the open period. Historical vouchers keep the GL rows they were reported
	with; the correction is its own auditable posting."""
	from erpnext.accounts.general_ledger import make_gl_entries
	from erpnext.stock import get_warehouse_account_map

	account_map = get_warehouse_account_map(args.get("company"))
	warehouse_account = (account_map.get(event_row.warehouse) or {}).get("account")
	if not warehouse_account:
		return  # no perpetual stock GL on this warehouse: nothing to correct

	excluded = args.get("exclude_voucher") or (args.get("voucher_type"), args.get("voucher_no"))
	deltas: dict[tuple[str, str], float] = {}
	for sle_name, projection in projections.items():
		stored = live.get(sle_name)
		if stored is None or (stored.voucher_type, stored.voucher_no) == tuple(excluded):
			continue

		delta = flt(projection["svd"]) - flt(stored.stock_value_difference)
		if abs(delta) < 0.005:
			continue

		counter = _counter_account(stored.voucher_type, stored.voucher_no, warehouse_account)
		if counter:
			key = (counter, str(stored.posting_date))
			deltas[key] = deltas.get(key, 0.0) + delta

	gl_map = []
	for (counter, posting_date), delta in sorted(deltas.items()):
		gl_map.append(_adjustment_row(args, warehouse_account, counter, delta, posting_date))
		gl_map.append(_adjustment_row(args, counter, warehouse_account, -delta, posting_date))

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


def _adjustment_row(args: dict, account: str, against: str, debit: float, posting_date: str) -> frappe._dict:
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
			"posting_date": posting_date,
			"cost_center": frappe.get_cached_value("Company", args.get("company"), "cost_center"),
			"remarks": args.get("adjustment_remark") or "Stock value adjustment for backdated entry",
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
		if (
			row.kind in ("Reversal", "Revaluation")
			and row.reverses_event
			and cint(row.reverses_event) < window_start_id
		):
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
	if not _key_has_bundles(key):
		return True
	if not allow_lots:
		return False
	return not frappe.db.exists("Stock Event", {**key, "kind": "Assertion"})


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
		"exclude_voucher": (source.voucher_type, source.voucher_no),
		"adjustment_remark": "Stock value adjustment for landed cost",
	}
	outcome = _refold(engine, policy, event_row, args, allow_negative_stock=True)
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
	return _history_foldable({"item_code": item_code, "warehouse": warehouse}, allow_lots=True)


def post_revaluation_gl(
	company: str,
	warehouse: str,
	value_change: float,
	posting_date: str,
	voucher_type: str,
	voucher_no: str,
	credit_account: str,
) -> None:
	"""The source-side GL of a revaluation: stock up, expense account down,
	carried on the revising voucher, dated at the revalued receipt."""
	from erpnext.accounts.general_ledger import make_gl_entries
	from erpnext.stock import get_warehouse_account_map

	warehouse_account = (get_warehouse_account_map(company).get(warehouse) or {}).get("account")
	if not warehouse_account:
		return

	args = {"voucher_type": voucher_type, "voucher_no": voucher_no, "company": company}
	make_gl_entries(
		[
			_adjustment_row(args, warehouse_account, credit_account, value_change, posting_date),
			_adjustment_row(args, credit_account, warehouse_account, -value_change, posting_date),
		]
	)


def invalidate(item_code: str, warehouse: str) -> None:
	"""Drop the key's checkpoint after a legacy rewrite; next fold rebuilds it."""
	frappe.db.delete("Stock Fold State", {"item_code": item_code, "warehouse": warehouse})


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
			"value_change",
				"sle",
			],
			order_by="posting_datetime, name",
		)
		if cint(row.name) != cint(event_row.name)
	]

	bundle_rows = _bundle_backed_sles(key)
	allocations = _allocations([row.name for row in rows])
	try:
		events_list = [
			stock_engine_bridge.to_event(
				engine, row, allocations.get(str(row.name)) if row.sle in bundle_rows else None
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


def _save_state(engine, event_row: frappe._dict, state, checkpoint: str | None = None) -> None:
	from erpnext.stock.services import stock_engine_bridge

	payload = {
		"last_event": cint(event_row.name),
		"state_json": json.dumps(stock_engine_bridge.serialize_state(state)),
	}
	existing = checkpoint or frappe.db.get_value(
		"Stock Fold State", {"item_code": event_row.item_code, "warehouse": event_row.warehouse}, "name"
	)
	if existing:
		frappe.db.set_value("Stock Fold State", existing, payload, update_modified=True)
		return

	timestamp = frappe.utils.now()
	row = {
		"name": frappe.generate_hash(length=10),
		"item_code": event_row.item_code,
		"warehouse": event_row.warehouse,
		**payload,
		"creation": timestamp,
		"modified": timestamp,
		"owner": "Administrator",
		"modified_by": "Administrator",
	}
	frappe.db.bulk_insert("Stock Fold State", tuple(row), [list(row.values())])
