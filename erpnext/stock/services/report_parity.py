# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Report-level shadow mode: legacy report vs its fold-based port, row by row.

The valuation shadow proved the engine; this proves the read surface. Each
ported report runs side by side with its legacy original on the same filters
and diffs per key, so report migration carries the same evidence standard as
the engine did.

	bench --site <site> execute erpnext.stock.services.report_parity.stock_balance \
		--kwargs "{'company': '...', 'from_date': '2025-01-01', 'to_date': '2025-12-31'}"
"""

import frappe
from frappe.utils import flt

QTY_TOLERANCE = 1e-3
VALUE_TOLERANCE = 0.5
MAX_EXAMPLES = 25


def stock_balance(company: str, from_date: str, to_date: str, warehouse: str | None = None) -> dict:
	from erpnext.stock.report.stock_balance.stock_balance import execute as legacy_execute
	from erpnext.stock.report.stock_balance_fold.stock_balance_fold import execute as fold_execute

	filters = frappe._dict(company=company, from_date=from_date, to_date=to_date)
	if warehouse:
		filters.warehouse = warehouse

	legacy_rows = _index(legacy_execute(frappe._dict(filters))[1])
	fold_rows = _index(fold_execute(frappe._dict(filters))[1])

	report = {
		"legacy_rows": len(legacy_rows),
		"fold_rows": len(fold_rows),
		"compared": 0,
		"qty_matched": 0,
		"value_matched": 0,
		"only_legacy": [],
		"only_fold": [],
		"qty_mismatches": [],
		"value_mismatches": [],
	}

	for key in sorted(set(legacy_rows) | set(fold_rows)):
		legacy, fold = legacy_rows.get(key), fold_rows.get(key)
		if legacy is None:
			if len(report["only_fold"]) < MAX_EXAMPLES:
				report["only_fold"].append(key)
			continue
		if fold is None:
			if len(report["only_legacy"]) < MAX_EXAMPLES:
				report["only_legacy"].append(key)
			continue

		report["compared"] += 1
		if abs(flt(legacy.get("bal_qty")) - flt(fold.get("bal_qty"))) <= QTY_TOLERANCE:
			report["qty_matched"] += 1
			if abs(flt(legacy.get("bal_val")) - flt(fold.get("bal_val"))) <= VALUE_TOLERANCE:
				report["value_matched"] += 1
			elif len(report["value_mismatches"]) < MAX_EXAMPLES:
				report["value_mismatches"].append(
					{"key": key, "legacy": flt(legacy.get("bal_val")), "fold": flt(fold.get("bal_val"))}
				)
		elif len(report["qty_mismatches"]) < MAX_EXAMPLES:
			report["qty_mismatches"].append(
				{"key": key, "legacy": flt(legacy.get("bal_qty")), "fold": flt(fold.get("bal_qty"))}
			)

	report["only_legacy_count"] = len(report["only_legacy"])
	report["only_fold_count"] = len(report["only_fold"])
	return report


def _index(rows) -> dict:
	indexed = {}
	for row in rows:
		row = row if isinstance(row, dict) else row.__dict__
		key = (row.get("item_code"), row.get("warehouse"))
		if key[0] and key[1]:
			indexed[key] = row
	return indexed
