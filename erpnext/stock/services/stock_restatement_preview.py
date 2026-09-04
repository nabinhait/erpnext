# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Preview the Phase 4 lot-valuation restatement — the one irreversible step.

For every key that carries lot allocations, folds the same event history
twice: once at aggregate level (non-batchwise valuation, today's default for
old items) and once with lot sub-states (specific identification — the
unified model). The per-key value difference is exactly what the one-time
``use_batchwise_valuation`` restatement would post, so the numbers can be
reviewed per company before anything irreversible runs.

    bench --site <site> execute erpnext.stock.services.stock_restatement_preview.run
"""

import frappe
from frappe.utils import flt

from erpnext.stock.services import stock_engine_bridge
from erpnext.stock.services.stock_shadow import _allocations_by_event, _event_rows

MAX_KEYS = 200


def run(warehouses: list[str] | None = None, value_tolerance: float = 0.01) -> dict:
	engine = stock_engine_bridge.engine()
	report = {"keys_with_lots": 0, "keys_restated": 0, "total_delta": 0.0, "keys": [], "errors": []}

	for item_code, warehouse in _lot_keys(warehouses):
		policy = stock_engine_bridge.policy_for(item_code, engine)
		if policy is None:
			continue

		report["keys_with_lots"] += 1
		rows = _event_rows(item_code, warehouse)
		allocations = _allocations_by_event([row.name for row in rows])

		try:
			aggregate = engine.replay(
				[stock_engine_bridge.to_event(engine, row) for row in rows],
				engine.FoldContext(policy=policy),
			).final
			lot_level = engine.replay(
				[
					stock_engine_bridge.to_event(
						engine, row, allocations.get(str(row.name)), honor_batch_flag=False
					)
					for row in rows
				],
				engine.FoldContext(policy=policy),
			).final
		except ValueError as error:
			report["errors"].append((item_code, warehouse, str(error)))
			continue

		delta = flt(lot_level.value - aggregate.value, 6)
		if abs(delta) <= value_tolerance:
			continue

		report["keys_restated"] += 1
		report["total_delta"] += delta
		if len(report["keys"]) < MAX_KEYS:
			report["keys"].append(
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"aggregate_value": aggregate.value,
					"lot_level_value": lot_level.value,
					"delta": delta,
				}
			)

	return report


def _lot_keys(warehouses: list[str] | None) -> list[tuple[str, str]]:
	"""Keys whose event history carries at least one lot allocation."""
	event = frappe.qb.DocType("Stock Event")
	allocation = frappe.qb.DocType("Stock Event Allocation")
	query = (
		frappe.qb.from_(allocation)
		.join(event)
		.on(allocation.parent == event.name)
		.select(event.item_code, event.warehouse)
		.distinct()
		.orderby(event.item_code)
		.orderby(event.warehouse)
	)
	if warehouses:
		query = query.where(event.warehouse.isin(warehouses))

	return [(row.item_code, row.warehouse) for row in query.run(as_dict=True)]
