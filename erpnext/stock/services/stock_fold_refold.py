# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Refolding a key: rewrite the projections of every row a change in history
touches, from one core shared by three callers.

- ``refold_for_event`` — synchronous, anchored on the inserted or revaluing
  event (backdates and landed cost). Past ``REFOLD_CAP`` events since the
  baseline it values the new row from the nearest checkpoint, shifts future
  quantities as legacy does, and queues the rest as a Stock Refold.
- ``refold_key`` — background, from an instant, no cap: the Stock Refold
  queue and the Stock Restatement job.

The window a refold rewrites starts at the last assertion at or before the
anchor (an assertion reconstructs the exact state) and stops after the first
assertion beyond it. GL corrections are append-only rows carried on the
triggering voucher, dated at each affected voucher's own posting date inside
the open period.
"""

import json

import frappe
from frappe.utils import cint, flt

from erpnext.stock.services import stock_engine_bridge

REFOLD_CAP = 20000
ASSERTING = ("Assertion",)


def refold_for_event(
	engine, policy, event_row: frappe._dict, args: dict, allow_negative_stock: bool
) -> str | None:
	"""Backdated insert or revaluation. Returns REFOLDED (done in place),
	QUEUED (own row valued, rest queued) or None (legacy engine)."""
	from erpnext.stock.services import stock_fold_authority as authority

	key = {"item_code": event_row.item_code, "warehouse": event_row.warehouse}
	reason = authority.foldable_reason(key, allow_lots=True)
	if reason == "cap":
		return _value_now_queue_rest(engine, policy, event_row, args, allow_negative_stock)
	if reason:
		return None

	baseline = authority._latest_baseline(key)
	if baseline and str(event_row.posting_datetime) < str(baseline):
		return None  # backdates into the frozen era stay legacy

	anchor = (str(event_row.posting_datetime), cint(event_row.name))
	rows = _rows_since(key, baseline)
	return _refold_rows(engine, policy, key, rows, anchor, args, validate=(event_row, allow_negative_stock))


def refold_key(item_code: str, warehouse: str, from_datetime: str, args: dict) -> bool:
	"""Refold everything at or after the instant, however deep. False when the
	key cannot fold at all (its legacy values stay)."""
	from erpnext.stock.services import stock_fold_authority as authority

	engine = stock_engine_bridge.engine()
	policy = stock_engine_bridge.policy_for(item_code, engine)
	key = {"item_code": item_code, "warehouse": warehouse}
	if policy is None or authority.foldable_reason(key, allow_lots=True, ignore_cap=True):
		return False

	baseline = authority._latest_baseline(key)
	start = max(str(from_datetime), str(baseline)) if baseline else str(from_datetime)
	rows = _rows_since(key, baseline)
	return _refold_rows(engine, policy, key, rows, (start, 0), args) is not None


def _value_now_queue_rest(engine, policy, event_row, args: dict, allow_negative_stock: bool) -> str | None:
	"""Legacy's own shape for a deep backdate: the inserted row is valued
	synchronously (from the nearest checkpoint), quantities shift now, values
	of later rows follow in the background."""
	from erpnext.stock.doctype.stock_refold.stock_refold import enqueue_refold
	from erpnext.stock.services import stock_fold_authority as authority
	from erpnext.stock.services import stock_fold_read

	state = stock_fold_read.state_before(engine, event_row)
	allocations = None
	if args.get("serial_and_batch_bundle"):
		allocations = authority._allocations([event_row.name]).get(str(event_row.name))
	try:
		event = stock_engine_bridge.to_event(engine, event_row, allocations)
	except ValueError:
		return None

	result = engine.replay([event], engine.FoldContext(policy=policy), start=state)
	effect = result.effects[event.id]
	authority._validate_negative(effect, args, allow_negative_stock)
	authority._project_sle(event_row.sle, result.final, effect, policy, engine)
	enqueue_refold(
		event_row.item_code,
		event_row.warehouse,
		args.get("company"),
		event_row.posting_datetime,
		args.get("voucher_type"),
		args.get("voucher_no"),
	)
	return authority.QUEUED


def _rows_since(key: dict, baseline: str | None) -> list[frappe._dict]:
	from erpnext.stock.services import stock_fold_authority as authority

	filters = dict(key)
	if baseline:
		filters["posting_datetime"] = (">=", str(baseline))
	return authority._drop_revoked_baselines(
		frappe.get_all(
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
	)


def _refold_rows(
	engine, policy, key: dict, rows: list, anchor: tuple, args: dict, validate=None
) -> str | None:
	"""Fold the window around the anchor and rewrite what changed. ``validate``
	is (event_row, allow_negative_stock) for the synchronous event path."""
	from erpnext.stock.services import stock_fold_authority as authority

	window = _refold_window(rows, anchor)
	if window is None:
		return authority.REFOLDED  # nothing at or after the anchor
	is_tail = window[1] == len(rows)
	rows = rows[window[0] : window[1]]
	# a boundary assertion reconstructs the state but its own stored values are
	# untouched by the change — never re-project it
	changed_rows = rows[1:] if window[0] > 0 else rows

	bundle_rows = authority._bundle_backed_sles(key)
	allocations = authority._allocations([row.name for row in rows])
	try:
		events = [
			stock_engine_bridge.to_event(
				engine,
				row,
				allocations.get(str(row.name))
				if row.sle in bundle_rows or authority._is_baseline(row)
				else None,
			)
			for row in rows
		]
	except ValueError:
		return None

	result = engine.replay(events, engine.FoldContext(policy=policy))
	if validate:
		event_row, allow_negative_stock = validate
		authority._validate_negative(result.effects[cint(event_row.name)], args, allow_negative_stock)

	live = _live_rows(key, changed_rows)
	start_value = 0.0
	if window[0] > 0:
		start_value = _equivalent_value(result.states[cint(rows[0].name)])
	projections = _absorbed_projections(changed_rows, result, start_value)
	for sle_name, projection in projections.items():
		if sle_name in live:
			_project_sle_values(sle_name, projection, policy, engine)

	if is_tail:
		# the window reaches the present, so latest state and checkpoint move
		authority._project_bin(frappe._dict(key), result.final)
		last_id = max(cint(row.name) for row in rows)
		authority._save_state(engine, frappe._dict({**key, "name": last_id}), result.final)

	# history changed at this instant: checkpoints photographed at or after it
	# are stale and must never seed a read; they rebuild at the next closing
	# or scheduled run
	frappe.db.delete("Stock Fold Checkpoint", {**key, "as_of": (">=", anchor[0])})
	_correct_gl(args, key, anchor[0], projections, live)
	return authority.REFOLDED


def _correct_gl(args: dict, key: dict, instant: str, projections: dict, live: dict) -> None:
	from erpnext.stock.services import stock_fold_authority as authority

	if frappe.conf.get(authority.GL_ADJUSTMENT_FLAG) or args.get("force_gl_adjustment"):
		if not args.get("skip_gl_adjustment"):
			_post_gl_adjustment(args, key, projections, live)
	elif frappe.conf.get(authority.SUPPRESS_FLAG):
		_regenerate_gl(args, instant, live.values())


def _live_rows(key: dict, changed_rows: list) -> dict:
	names = [row.sle for row in changed_rows if row.sle]
	if not names:
		return {}
	return {
		sle.name: sle
		for sle in frappe.get_all(
			"Stock Ledger Entry",
			filters={**key, "is_cancelled": 0, "name": ("in", names)},
			fields=["name", "voucher_type", "voucher_no", "posting_date", "stock_value_difference"],
		)
	}


def _refold_window(rows: list, anchor: tuple) -> tuple[int, int] | None:
	"""The slice of history a change at the anchor can actually reach, or
	None when no row lies at or after it.

	An assertion pins quantity and value, so the refold starts at the last
	assertion at or before the anchor (folded from empty, it reconstructs
	the exact state) and stops after the first assertion beyond it. A
	reversal or revaluation referencing an event before the window forces a
	full refold — its source layer lives outside the slice."""
	keys = [(str(row.posting_datetime), cint(row.name)) for row in rows]
	inserted = next((index for index, sort_key in enumerate(keys) if sort_key >= anchor), None)
	if inserted is None:
		return None

	start = 0
	for index in range(inserted, -1, -1):
		if rows[index].kind in ASSERTING:
			start = index
			break

	end = len(rows)
	for index in range(inserted + 1, len(rows)):
		if rows[index].kind in ASSERTING:
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


def _open_period_date(posting_date, closed_until, fallback) -> str:
	"""Adjustments never post into a closed accounting period: dates at or
	before the latest Period Closing Voucher clamp to the revising voucher's
	own date (which passed close validation at submit)."""
	if closed_until and str(posting_date) <= str(closed_until):
		return str(fallback)
	return str(posting_date)


def _closed_until(company: str):
	return frappe.db.get_value(
		"Period Closing Voucher",
		{"docstatus": 1, "company": company},
		"period_end_date",
		order_by="period_end_date desc",
	)


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


def _post_gl_adjustment(args: dict, key: dict, projections: dict, live: dict) -> None:
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
	warehouse_account = (account_map.get(key["warehouse"]) or {}).get("account")
	if not warehouse_account:
		return  # no perpetual stock GL on this warehouse: nothing to correct

	excluded = args.get("exclude_voucher") or (args.get("voucher_type"), args.get("voucher_no"))
	closed_until = _closed_until(args.get("company"))
	fallback_date = args.get("posting_date") or frappe.utils.nowdate()
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
			key = (counter, _open_period_date(stored.posting_date, closed_until, fallback_date))
			deltas[key] = deltas.get(key, 0.0) + delta

	gl_map = []
	for (counter, posting_date), delta in sorted(deltas.items()):
		gl_map.append(adjustment_row(args, warehouse_account, counter, delta, posting_date))
		gl_map.append(adjustment_row(args, counter, warehouse_account, -delta, posting_date))

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


def adjustment_row(args: dict, account: str, against: str, debit: float, posting_date: str) -> frappe._dict:
	"""One GL row of a stock value adjustment carried on the voucher in args."""
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


def _regenerate_gl(args: dict, instant: str, live_sles) -> None:
	"""With the legacy repost suppressed, correct affected vouchers' GL inline.

	Comparison-based regeneration: only vouchers whose GL no longer matches
	their (refolded) svd get rewritten. The voucher being submitted is
	excluded — its GL posts normally later in the same submit."""
	from erpnext.accounts.utils import repost_gle_for_stock_vouchers

	current = (args.get("voucher_type"), args.get("voucher_no"))
	vouchers = sorted({(sle.voucher_type, sle.voucher_no) for sle in live_sles} - {current})
	repost_gle_for_stock_vouchers(vouchers, str(instant)[:10], company=args.get("company"))


def _equivalent_value(state) -> float:
	return stock_engine_bridge.equivalent_value(state)
