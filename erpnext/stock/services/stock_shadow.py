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

from erpnext.stock.services import stock_engine_bridge

QTY_TOLERANCE = 1e-6
VALUE_TOLERANCE = 0.05
MAX_EXAMPLES = 50


def run(
	warehouses: list[str] | None = None,
	qty_tolerance: float = QTY_TOLERANCE,
	value_tolerance: float = VALUE_TOLERANCE,
	shard: int | None = None,
	shards: int | None = None,
) -> dict:
	engine = stock_engine_bridge.engine()
	report = {
		"keys": 0,
		"events": 0,
		"matched": 0,
		"precision_noise": 0,
		"class_a_keys": [],
		"batchwise_keys": [],
		"batchwise_key_count": 0,
		"hybrid_keys": [],
		"hybrid_key_count": 0,
		"class_b": [],
		"negative_exposure": [],
		"skipped_standard_cost": [],
		"conversion_errors": [],
	}

	keys = _keys(warehouses)
	if shards:
		keys = keys[shard::shards]

	for index, (item_code, warehouse) in enumerate(keys):
		if index and index % 5000 == 0:
			print(f"[shadow] {index}/{len(keys)} keys", flush=True)
		policy = stock_engine_bridge.policy_for(item_code, engine)
		if policy is None:
			report["skipped_standard_cost"].append((item_code, warehouse))
			continue

		report["keys"] += 1
		rows = _event_rows(item_code, warehouse)
		legacy = _legacy_rows([row.sle for row in rows if row.sle])
		legacy_broken = _legacy_inconsistent(rows, legacy, qty_tolerance)
		if legacy_broken:
			report["class_a_keys"].append((item_code, warehouse))

		allocations = _allocations_by_event([row.name for row in rows])
		try:
			events = [
				stock_engine_bridge.to_event(
					engine,
					row,
					allocations.get(str(row.name))
					if (legacy.get(row.sle) or {}).get("serial_and_batch_bundle")
					else None,
				)
				for row in rows
			]
		except ValueError as error:
			report["conversion_errors"].append((item_code, warehouse, str(error)))
			continue

		result = engine.replay(events, engine.FoldContext(policy=policy))

		# mixed-era histories: keys reposted after the v15 migration were
		# restated batch-wise by legacy itself. If aggregate folding leaves
		# mismatches and the key carries lot facts, try per-lot — matching
		# either of legacy's own semantics is a pass, recorded per key.
		if allocations and _has_misfits(rows, result, legacy, qty_tolerance, value_tolerance):
			try:
				lot_events = [
					stock_engine_bridge.to_event(engine, row, allocations.get(str(row.name)))
					for row in rows
				]
				lot_result = engine.replay(lot_events, engine.FoldContext(policy=policy))
				if not _has_misfits(rows, lot_result, legacy, qty_tolerance, value_tolerance):
					result = lot_result
					if len(report["batchwise_keys"]) < MAX_EXAMPLES:
						report["batchwise_keys"].append((item_code, warehouse))
					report["batchwise_key_count"] += 1
				else:
					hybrid = _match_hybrid(rows, result, lot_result, legacy, value_tolerance)
					if hybrid is not None:
						boundary, offset = hybrid
						report["hybrid_key_count"] += 1
						if len(report["hybrid_keys"]) < MAX_EXAMPLES:
							report["hybrid_keys"].append(
								{
									"item_code": item_code,
									"warehouse": warehouse,
									"switchover": str(rows[boundary].posting_datetime),
									"offset": round(offset, 4),
									"riv_found": bool(
										frappe.db.exists(
											"Repost Item Valuation",
											{
												"item_code": item_code,
												"warehouse": warehouse,
												"docstatus": 1,
											},
										)
									),
								}
							)
						report["events"] += len(rows)
						report["matched"] += sum(1 for row in rows if row.sle in legacy)
						continue
			except ValueError:
				pass

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


def _match_hybrid(rows, agg_result, lot_result, legacy, value_tolerance):
	"""Detect a partially-reposted key: aggregate values up to a boundary, then
	batchwise values seeded from the boundary's stored balance.

	Legacy's repost re-derives each restated row from the stored previous
	balance while consuming at per-batch rates, so the suffix equals the
	per-lot fold shifted by a constant: the aggregate-vs-lot value gap at the
	boundary. Returns (boundary_index, offset) or None."""
	band = value_tolerance * 10
	boundary = None
	for index, row in enumerate(rows):
		stored = legacy.get(row.sle)
		if stored is None:
			continue
		agg_value = _legacy_equivalent_value(agg_result.states[cint(row.name)])
		if abs(agg_value - flt(stored.stock_value)) > band:
			boundary = index
			break

	if not boundary:  # no divergence, or diverged on the very first row
		return None

	previous = next(
		(legacy.get(rows[i].sle) for i in range(boundary - 1, -1, -1) if legacy.get(rows[i].sle)), None
	)
	if previous is None:
		return None
	offset = flt(previous.stock_value) - _legacy_equivalent_value(
		lot_result.states[cint(rows[boundary - 1].name)]
	)

	for row in rows[boundary:]:
		stored = legacy.get(row.sle)
		if stored is None:
			continue
		lot_value = _legacy_equivalent_value(lot_result.states[cint(row.name)])
		if abs(lot_value + offset - flt(stored.stock_value)) > band:
			return None

	return boundary, offset


def _has_misfits(rows, result, legacy, qty_tolerance, value_tolerance) -> bool:
	for row in rows:
		effect = result.effects.get(cint(row.name))
		stored = legacy.get(row.sle)
		if effect is None or stored is None:
			continue
		state = result.states[cint(row.name)]
		if abs(effect.qty_after - flt(stored.qty_after_transaction)) > qty_tolerance * 1000:
			return True
		if abs(_legacy_equivalent_value(state) - flt(stored.stock_value)) > value_tolerance * 10:
			return True
	return False


def _allocations_by_event(event_names: list) -> dict[str, list[frappe._dict]]:
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


def _legacy_rows(sle_names: list[str]) -> dict[str, frappe._dict]:
	if not sle_names:
		return {}
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters={"name": ("in", sle_names)},
		fields=[
			"name",
			"actual_qty",
			"qty_after_transaction",
			"stock_value",
			"voucher_type",
			"serial_and_batch_bundle",
		],
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


def diagnose(item_code: str, warehouse: str, limit: int = 12) -> list[dict]:
	"""Row-by-row legacy vs fold comparison for one key, from the first divergence."""
	engine = stock_engine_bridge.engine()
	policy = stock_engine_bridge.policy_for(item_code, engine)
	rows = _event_rows(item_code, warehouse)
	allocations = _allocations_by_event([row.name for row in rows])
	events = [stock_engine_bridge.to_event(engine, row, allocations.get(str(row.name))) for row in rows]
	result = engine.replay(events, engine.FoldContext(policy=policy))

	legacy = {
		row.name: row
		for row in frappe.get_all(
			"Stock Ledger Entry",
			filters={"name": ("in", [row.sle for row in rows if row.sle])},
			fields=[
				"name",
				"voucher_type",
				"actual_qty",
				"incoming_rate",
				"outgoing_rate",
				"qty_after_transaction",
				"stock_value",
				"stock_value_difference",
			],
		)
	}

	out = []
	diverged = False
	for row in rows:
		stored = legacy.get(row.sle)
		effect = result.effects.get(cint(row.name))
		if not stored or not effect:
			continue
		delta = abs(_legacy_equivalent_value(result.states[cint(row.name)]) - flt(stored.stock_value))
		if not diverged and delta <= 0.05:
			continue
		diverged = True
		out.append(
			{
				"sle": row.sle,
				"voucher_type": stored.voucher_type,
				"qty": stored.actual_qty,
				"in_rate": stored.incoming_rate,
				"out_rate": stored.outgoing_rate,
				"legacy_value": stored.stock_value,
				"legacy_svd": stored.stock_value_difference,
				"fold_value": effect.value_after,
				"fold_delta": effect.value_delta,
			}
		)
		if len(out) >= limit:
			break
	return out
