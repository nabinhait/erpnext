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
		else:
			report["qty_mismatches"].append(
				{"key": key, "legacy": flt(legacy.get("bal_qty")), "fold": flt(fold.get("bal_qty"))}
			)

	report["only_legacy_count"] = len(report["only_legacy"])
	report["only_fold_count"] = len(report["only_fold"])
	_arbitrate(report, to_date)
	return report


def _arbitrate(report: dict, to_date: str) -> None:
	"""Referee disputed keys against the ledger itself: the live SLE quantity
	sum up to the boundary is ground truth no report can argue with."""
	verdicts = {"legacy_wrong": 0, "fold_wrong": 0, "both_wrong": 0}
	for mismatch in report["qty_mismatches"]:
		item_code, warehouse = mismatch["key"]
		truth = flt(
			frappe.db.sql(
				"""SELECT SUM(actual_qty) FROM `tabStock Ledger Entry`
				WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND posting_date<=%s""",
				(item_code, warehouse, to_date),
			)[0][0]
		)
		mismatch["truth"] = truth
		legacy_ok = abs(mismatch["legacy"] - truth) <= QTY_TOLERANCE
		fold_ok = abs(mismatch["fold"] - truth) <= QTY_TOLERANCE
		if fold_ok and not legacy_ok:
			verdicts["legacy_wrong"] += 1
		elif legacy_ok and not fold_ok:
			verdicts["fold_wrong"] += 1
		elif not legacy_ok and not fold_ok:
			verdicts["both_wrong"] += 1
	report["qty_arbitration"] = verdicts
	report["qty_mismatches"] = report["qty_mismatches"][:MAX_EXAMPLES]


def _index(rows) -> dict:
	indexed = {}
	for row in rows:
		row = row if isinstance(row, dict) else row.__dict__
		key = (row.get("item_code"), row.get("warehouse"))
		if key[0] and key[1]:
			indexed[key] = row
	return indexed
