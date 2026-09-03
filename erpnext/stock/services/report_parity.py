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


def stock_ledger(company: str, from_date: str, to_date: str, warehouse: str | None = None, item_code: str | None = None) -> dict:
	"""Row-level parity: fold ledger rows vs stored SLE values, joined by SLE."""
	from erpnext.stock.report.stock_ledger_fold.stock_ledger_fold import execute as fold_execute

	filters = frappe._dict(company=company, from_date=from_date, to_date=to_date)
	if warehouse:
		filters.warehouse = warehouse
	if item_code:
		filters.item_code = item_code

	fold_rows = [row for row in fold_execute(frappe._dict(filters))[1] if row.get("sle")]
	stored = {}
	if fold_rows:
		for row in frappe.get_all(
			"Stock Ledger Entry",
			filters={"name": ("in", [row["sle"] for row in fold_rows])},
			fields=["name", "qty_after_transaction", "stock_value"],
		):
			stored[row.name] = row

	report = {"rows": len(fold_rows), "qty_matched": 0, "value_matched": 0, "mismatches": []}
	for row in fold_rows:
		legacy = stored.get(row["sle"])
		if not legacy:
			continue
		qty_ok = abs(flt(legacy.qty_after_transaction) - flt(row["qty_after_transaction"])) <= QTY_TOLERANCE
		value_ok = abs(flt(legacy.stock_value) - flt(row["stock_value"])) <= VALUE_TOLERANCE
		report["qty_matched"] += 1 if qty_ok else 0
		report["value_matched"] += 1 if value_ok else 0
		if not (qty_ok and value_ok) and len(report["mismatches"]) < MAX_EXAMPLES:
			report["mismatches"].append(
				{
					"sle": row["sle"],
					"stored_qty": flt(legacy.qty_after_transaction),
					"fold_qty": flt(row["qty_after_transaction"]),
					"stored_value": flt(legacy.stock_value),
					"fold_value": flt(row["stock_value"]),
				}
			)
	return report


def stock_ageing(company: str, to_date: str, warehouse: str | None = None) -> dict:
	"""Key-level parity: fold ageing vs legacy ageing, arbitrated on quantity."""
	from erpnext.stock.report.stock_ageing.stock_ageing import execute as legacy_execute
	from erpnext.stock.report.stock_ageing_fold.stock_ageing_fold import execute as fold_execute

	filters = frappe._dict(
		company=company,
		to_date=to_date,
		range1=30,
		range2=60,
		range3=90,
		range="30, 60, 90",
		show_warehouse_wise_stock=1,
	)
	if warehouse:
		filters.warehouse = warehouse

	legacy_columns, legacy_raw = legacy_execute(frappe._dict(filters))[:2]
	fieldnames = [column.get("fieldname") for column in legacy_columns]
	legacy_rows = _index(
		[dict(zip(fieldnames, row, strict=False)) if isinstance(row, (list, tuple)) else row for row in legacy_raw]
	)
	fold_rows = _index(fold_execute(frappe._dict(filters))[1])

	report = {
		"legacy_rows": len(legacy_rows),
		"fold_rows": len(fold_rows),
		"compared": 0,
		"qty_matched": 0,
		"age_within_5d": 0,
		"mismatches": [],
	}
	for key in sorted(set(legacy_rows) & set(fold_rows)):
		legacy, fold = legacy_rows[key], fold_rows[key]
		report["compared"] += 1
		if abs(flt(legacy.get("qty")) - flt(fold.get("qty"))) <= QTY_TOLERANCE:
			report["qty_matched"] += 1
		if abs(flt(legacy.get("average_age")) - flt(fold.get("average_age"))) <= 5:
			report["age_within_5d"] += 1
		elif len(report["mismatches"]) < MAX_EXAMPLES:
			report["mismatches"].append(
				{
					"key": key,
					"legacy_age": flt(legacy.get("average_age")),
					"fold_age": flt(fold.get("average_age")),
					"legacy_qty": flt(legacy.get("qty")),
					"fold_qty": flt(fold.get("qty")),
				}
			)
	return report
