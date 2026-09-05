# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, today

from erpnext.stock import get_warehouse_account_map
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.services import stock_ledger_writer
from erpnext.stock.services.stock_fold_authority import _latest_baseline
from erpnext.tests.utils import ERPNextTestSuite

COMPANY = "_Test Opening Adjustment Company"
ABBREVIATION = "TOAC"
CLOSING_ENQUEUE = "erpnext.stock.doctype.stock_closing_entry.stock_closing_entry.enqueue"


class TestStockOpeningAdjustment(ERPNextTestSuite):
	def test_frontier_adjustment_books_engine_truth(self):
		"""Legacy's stored balance drifted from what the facts say. The
		adjustment lists the key, books exactly the difference against the
		adjustment account on the first open day, pins the key at engine
		values so the fold continues from there, and is cancelled — books
		reversed, baseline revoked — only through its closing entry."""
		company = make_company(COMPANY, ABBREVIATION)
		warehouse = make_warehouse("Opening Adjustment WH", company)
		item = make_item(properties={"is_stock_item": 1, "valuation_method": "Moving Average"}).name
		key = {"item_code": item, "warehouse": warehouse}
		frontier = add_days(today(), -1)

		frappe.conf.stock_event_dual_write = 1
		try:
			make_stock_entry(
				item_code=item,
				target=warehouse,
				qty=10,
				basic_rate=100,
				company=company,
				posting_date=frontier,
			)
			make_stock_entry(item_code=item, source=warehouse, qty=4, company=company, posting_date=frontier)
			_drift_legacy_value(key, by=37)

			closing = submit_closing(company, frontier)
			adjustment = frappe.get_doc(
				doctype="Stock Opening Adjustment", company=company, stock_closing_entry=closing.name
			).insert()
			adjustment.build()

			self.assertEqual(adjustment.status, "Ready")
			self.assertEqual(adjustment.keys, 1)
			self.assertEqual(adjustment.posting_date, frappe.utils.getdate(today()))
			self.assertAlmostEqual(adjustment.total_delta, -37, places=2)
			row = adjustment.items[0]
			self.assertEqual((row.item_code, row.warehouse), (item, warehouse))
			self.assertAlmostEqual(row.engine_value, 600, places=2)
			self.assertAlmostEqual(row.legacy_value, 637, places=2)

			adjustment.submit()
			self._assert_gl_delta(adjustment, warehouse, -37)
			self.assertAlmostEqual(_bin_value(key), 600, places=2)
			baseline = frappe.db.get_value(
				"Stock Event",
				{**key, "source": "Baseline", "voucher_no": adjustment.name},
				["assert_qty", "assert_rate", "voucher_type"],
				as_dict=True,
			)
			self.assertEqual(baseline.voucher_type, "Stock Opening Adjustment")
			self.assertEqual(flt(baseline.assert_qty), 6)
			self.assertAlmostEqual(flt(baseline.assert_rate), 100, places=4)

			frappe.conf.stock_fold_authoritative = 1
			issue = make_stock_entry(item_code=item, source=warehouse, qty=1, company=company)
			sle = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": issue.name},
				["qty_after_transaction", "stock_value_difference"],
				as_dict=True,
			)
			self.assertEqual(flt(sle.qty_after_transaction), 5)
			self.assertAlmostEqual(flt(sle.stock_value_difference), -100, places=4)

			self.assertRaises(frappe.ValidationError, adjustment.cancel)
			adjustment.reload()
			self.assertEqual(adjustment.docstatus, 1)
			closing.reload()
			closing.cancel()
		finally:
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		adjustment.reload()
		self.assertEqual(adjustment.docstatus, 2)
		self.assertEqual(adjustment.status, "Cancelled")
		self._assert_gl_delta(adjustment, warehouse, 0)
		self.assertIsNone(_latest_baseline(key))

	def test_threshold_gates_auto_submit(self):
		"""within_threshold is the migration's go/no-go: set only when a
		threshold exists and the absolute delta fits inside it."""
		company = make_company(COMPANY, ABBREVIATION)
		warehouse = make_warehouse("Opening Threshold WH", company)
		item = make_item(properties={"is_stock_item": 1}).name
		frontier = add_days(today(), -1)

		frappe.conf.stock_event_dual_write = 1
		try:
			make_stock_entry(
				item_code=item, target=warehouse, qty=2, basic_rate=50, company=company, posting_date=frontier
			)
			_drift_legacy_value({"item_code": item, "warehouse": warehouse}, by=8)
			closing = submit_closing(company, frontier)
			adjustment = frappe.get_doc(
				doctype="Stock Opening Adjustment", company=company, stock_closing_entry=closing.name
			).insert()

			for threshold, expected in ((0, 0), (5, 0), (10, 1)):
				frappe.db.set_single_value("Stock Settings", "opening_adjustment_threshold", threshold)
				adjustment.build()
				self.assertAlmostEqual(adjustment.total_delta, -8, places=2)
				self.assertEqual(adjustment.within_threshold, expected, msg=f"threshold {threshold}")
		finally:
			frappe.conf.pop("stock_event_dual_write", None)
			frappe.db.set_single_value("Stock Settings", "opening_adjustment_threshold", 0)

	def _assert_gl_delta(self, adjustment, warehouse: str, expected: float) -> None:
		rows = frappe.get_all(
			"GL Entry", filters={"voucher_no": adjustment.name, "is_cancelled": 0}, pluck="posting_date"
		)
		self.assertTrue(all(posting_date == adjustment.posting_date for posting_date in rows))
		self.assertAlmostEqual(
			stock_account_balance(adjustment.company, warehouse, adjustment.name), expected, places=2
		)
		adjustment_balance = _account_balance(adjustment.adjustment_account, adjustment.name)
		self.assertAlmostEqual(adjustment_balance, -expected, places=2)


def make_company(name: str, abbreviation: str) -> str:
	if not frappe.db.exists("Company", name):
		frappe.get_doc(
			doctype="Company",
			company_name=name,
			abbr=abbreviation,
			default_currency="INR",
			country="India",
			chart_of_accounts="Standard",
			enable_perpetual_inventory=1,
		).insert()
	return name


def make_warehouse(name: str, company: str) -> str:
	abbreviation = frappe.get_cached_value("Company", company, "abbr")
	full_name = f"{name} - {abbreviation}"
	if not frappe.db.exists("Warehouse", full_name):
		frappe.get_doc(
			doctype="Warehouse",
			warehouse_name=name,
			company=company,
			parent_warehouse=f"All Warehouses - {abbreviation}",
		).insert()
	return full_name


def submit_closing(company: str, to_date: str):
	closing = frappe.get_doc(
		doctype="Stock Closing Entry", company=company, from_date=add_days(to_date, -30), to_date=to_date
	)
	with patch(CLOSING_ENQUEUE):
		closing.submit()
	return closing


def stock_account_balance(company: str, warehouse: str, voucher_no: str | None = None) -> float:
	"""Net debit of the warehouse's stock account, optionally for one voucher."""
	return _account_balance(get_warehouse_account_map(company)[warehouse].account, voucher_no)


def _account_balance(account: str, voucher_no: str | None = None) -> float:
	filters = {"account": account, "is_cancelled": 0}
	if voucher_no:
		filters["voucher_no"] = voucher_no
	rows = frappe.get_all("GL Entry", filters=filters, fields=["debit", "credit"])
	return flt(sum(flt(row.debit) - flt(row.credit) for row in rows))


def _drift_legacy_value(key: dict, by: float) -> None:
	"""Simulate legacy drift: the stored balance disagrees with the facts."""
	last = frappe.get_all(
		"Stock Ledger Entry",
		filters={**key, "is_cancelled": 0},
		fields=["name", "stock_value", "stock_value_difference"],
		order_by="posting_datetime desc, creation desc",
		limit=1,
	)[0]
	stock_ledger_writer.set_fields(
		last.name,
		{
			"stock_value": flt(last.stock_value) + by,
			"stock_value_difference": flt(last.stock_value_difference) + by,
		},
	)
	frappe.db.set_value("Bin", {**key}, "stock_value", flt(last.stock_value) + by)


def _bin_value(key: dict) -> float:
	return flt(frappe.db.get_value("Bin", key, "stock_value"))
