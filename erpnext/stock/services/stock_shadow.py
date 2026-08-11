# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Shadow mode: fold Stock Events with the pure engine, diff against legacy SLEs.

For every (item, warehouse) key with events, replays the facts through the
``stock_engine`` fold and compares each event's resulting quantity and stock
value against the values the legacy engine stored on the linked Stock Ledger
Entry. Mismatches are classified per the redesign doc:

- **(a) legacy inconsistent** — the legacy row already violates its own
  running-quantity invariant; disagreement there is expected, not a defect.
- **(b) genuine mismatch** — the classes shadow mode exists to surface.
- **(c) precision noise** — inside tolerance; counted, not reported.
- **negative exposure** — rows where the balance is negative: legacy invents
  a rate via its cascade and carries negative stock value; the engine holds
  the uncovered quantity as exposure (§2.10). Reported separately because
  replicate-vs-restate is a program decision, not a defect.

Requires the ``stock_engine`` app. Run from the bench console:

    bench --site <site> execute erpnext.stock.services.stock_shadow.run
"""

import frappe
from frappe.utils import cint, flt

from erpnext.stock.utils import get_valuation_method

QTY_TOLERANCE = 1e-6
VALUE_TOLERANCE = 0.05
MAX_EXAMPLES = 50


def run(
	warehouses: list[str] | None = None,
	qty_tolerance: float = QTY_TOLERANCE,
	value_tolerance: float = VALUE_TOLERANCE,
) -> dict:
	engine = _engine()
	report = {
		"keys": 0,
		"events": 0,
		"matched": 0,
		"precision_noise": 0,
		"class_a_keys": [],
		"class_b": [],
		"negative_exposure": [],
		"skipped_standard_cost": [],
		"conversion_errors": [],
	}

	for item_code, warehouse in _keys(warehouses):
		policy = _policy(engine, item_code)
		if policy is None:
			report["skipped_standard_cost"].append((item_code, warehouse))
			continue

		report["keys"] += 1
		rows = _event_rows(item_code, warehouse)
		legacy = _legacy_rows([row.sle for row in rows if row.sle])
		legacy_broken = _legacy_inconsistent(rows, legacy, qty_tolerance)
		if legacy_broken:
			report["class_a_keys"].append((item_code, warehouse))

		try:
			events = [_to_engine_event(engine, row) for row in rows]
		except ValueError as error:
			report["conversion_errors"].append((item_code, warehouse, str(error)))
			continue

		result = engine.replay(events, engine.FoldContext(policy=policy))

		for row in rows:
			report["events"] += 1
			effect = result.effects.get(cint(row.name))
			stored = legacy.get(row.sle)
			if effect is None or stored is None:
				continue

			shadow_value = _legacy_equivalent_value(result.states[cint(row.name)])
			qty_delta = abs(effect.qty_after - flt(stored.qty_after_transaction))
			value_delta = abs(shadow_value - flt(stored.stock_value))

			state = result.states[cint(row.name)]
			if qty_delta <= qty_tolerance and value_delta <= value_tolerance:
				report["matched"] += 1
			elif legacy_broken:
				pass  # already accounted under class (a)
			elif qty_delta <= qty_tolerance * 1000 and value_delta <= value_tolerance * 10:
				report["precision_noise"] += 1
			elif (effect.negative or state.exposure_qty) and qty_delta <= qty_tolerance:
				if len(report["negative_exposure"]) < MAX_EXAMPLES:
					report["negative_exposure"].append(
						{
							"sle": row.sle,
							"item_code": item_code,
							"warehouse": warehouse,
							"legacy_value": flt(stored.stock_value),
							"exposure_qty": state.exposure_qty,
						}
					)
			elif len(report["class_b"]) < MAX_EXAMPLES:
				report["class_b"].append(
					{
						"sle": row.sle,
						"item_code": item_code,
						"warehouse": warehouse,
						"legacy_qty": flt(stored.qty_after_transaction),
						"shadow_qty": effect.qty_after,
						"legacy_value": flt(stored.stock_value),
						"shadow_value": shadow_value,
					}
				)

	report["ok"] = not (report["class_b"] or report["conversion_errors"])
	return report


def _engine():
	try:
		from stock_engine.core.context import FoldContext
		from stock_engine.core.event import Event, EventKind
		from stock_engine.core.policies import Fifo, Lifo, MovingAverage
		from stock_engine.core.replay import replay
	except ImportError:
		frappe.throw("Shadow mode requires the stock_engine app to be installed on this bench")

	return frappe._dict(
		Event=Event,
		EventKind=EventKind,
		FoldContext=FoldContext,
		Fifo=Fifo,
		Lifo=Lifo,
		MovingAverage=MovingAverage,
		replay=replay,
	)


def _keys(warehouses: list[str] | None) -> list[tuple[str, str]]:
	filters = {"warehouse": ("in", warehouses)} if warehouses else {}
	rows = frappe.get_all(
		"Stock Event",
		filters=filters,
		fields=["item_code", "warehouse"],
		group_by="item_code, warehouse",
		order_by="item_code, warehouse",
	)
	return [(row.item_code, row.warehouse) for row in rows]


def _policy(engine, item_code: str):
	method = get_valuation_method(item_code)
	if method == "Standard Cost":
		return None  # fixed-rate semantics diverge by design; classified in M4's compat work
	if method == "LIFO":
		return engine.Lifo()
	if method == "Moving Average":
		return engine.MovingAverage()
	return engine.Fifo()


def _event_rows(item_code: str, warehouse: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Stock Event",
		filters={"item_code": item_code, "warehouse": warehouse},
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


def _legacy_rows(sle_names: list[str]) -> dict[str, frappe._dict]:
	if not sle_names:
		return {}
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters={"name": ("in", sle_names)},
		fields=["name", "actual_qty", "qty_after_transaction", "stock_value", "voucher_type"],
	)
	return {row.name: row for row in rows}


def _legacy_equivalent_value(state) -> float:
	"""Project the engine state onto legacy value semantics.

	The engine models a negative balance as exposure (provisional rate, value
	held at zero until covered — §2.10); legacy carries it as negative stock
	value. Legacy-equivalent value = value - exposure.
	"""
	return state.value - state.exposure_qty * state.exposure_rate


def _legacy_inconsistent(rows, legacy: dict, qty_tolerance: float) -> bool:
	"""True when the legacy rows break their own running-quantity invariant."""
	running = 0.0
	for row in rows:
		stored = legacy.get(row.sle)
		if stored is None:
			continue
		if stored.voucher_type == "Stock Reconciliation":
			running = flt(stored.qty_after_transaction)
			continue
		running += flt(stored.actual_qty)
		if abs(running - flt(stored.qty_after_transaction)) > qty_tolerance * 1000:
			return True
	return False


def _to_engine_event(engine, row: frappe._dict):
	kind = engine.EventKind(row.kind)

	if row.kind == "Reversal" and not row.reverses_event:
		# best-effort pairing failed; fold it as the movement it is
		kind = engine.EventKind.RECEIPT if flt(row.qty_change) > 0 else engine.EventKind.ISSUE

	return engine.Event(
		id=cint(row.name),
		posting_datetime=row.posting_datetime,
		kind=kind,
		qty_change=flt(row.qty_change),
		declared_rate=flt(row.declared_rate) if flt(row.qty_change) > 0 else None,
		assert_qty=flt(row.assert_qty) if row.kind == "Assertion" else None,
		assert_rate=flt(row.assert_rate) if row.kind == "Assertion" else None,
		reverses_event=cint(row.reverses_event) or None,
	)
