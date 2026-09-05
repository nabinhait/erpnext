# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, today

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.stock_opening_adjustment.test_stock_opening_adjustment import (
	CLOSING_ENQUEUE,
	_drift_legacy_value,
	make_company,
	make_warehouse,
	stock_account_balance,
	submit_closing,
)
from erpnext.stock.doctype.stock_restatement import stock_restatement
from erpnext.tests.utils import ERPNextTestSuite

COMPANY = "_Test Restatement Company"
ABBREVIATION = "TRSC"


class TestStockRestatement(ERPNextTestSuite):
	def test_reopening_the_frontier_restates_the_year(self):
		"""Cancelling the frontier closing queues a restatement that locks the
		period, slides the frontier one closing back (new closing + opening
		adjustment), refolds every key to engine truth with the corrections
		booked on the restatement, and unlocks when done."""
		company = make_company(COMPANY, ABBREVIATION)
		warehouse = make_warehouse("Restatement WH", company)
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
			issue = make_stock_entry(
				item_code=item, source=warehouse, qty=4, company=company, posting_date=frontier
			)
			_drift_legacy_value(key, by=37)
			closing, adjustment = self._frontier(company, frontier)

			with patch(CLOSING_ENQUEUE), patch.object(stock_restatement, "enqueue"):
				closing.reload()
				closing.cancel()
			name = frappe.db.get_value("Stock Restatement", {"cancelled_closing_entry": closing.name}, "name")
			restatement = frappe.get_doc("Stock Restatement", name)
			self.assertEqual(restatement.status, "Queued")
			self.assertEqual(str(restatement.to_date), frontier)
			self.assertLess(str(restatement.from_date), frontier)

			# the reopened period is locked while the restatement runs
			self.assertRaises(
				frappe.ValidationError,
				make_stock_entry,
				item_code=item,
				target=warehouse,
				qty=1,
				basic_rate=100,
				company=company,
				posting_date=frontier,
			)

			with patch(CLOSING_ENQUEUE):
				stock_restatement.run_restatement(name)
			restatement.reload()
			self.assertEqual(restatement.status, "Completed", msg=restatement.error)
			self.assertEqual(
				(restatement.keys_total, restatement.keys_done, restatement.keys_failed), (1, 1, 0)
			)

			new_closing = frappe.db.get_value(
				"Stock Closing Entry",
				restatement.frontier_closing_entry,
				["docstatus", "to_date"],
				as_dict=True,
			)
			self.assertEqual(
				(new_closing.docstatus, str(new_closing.to_date)), (1, str(restatement.from_date))
			)
			new_adjustment = frappe.db.get_value(
				"Stock Opening Adjustment",
				restatement.opening_adjustment,
				["docstatus", "keys"],
				as_dict=True,
			)
			self.assertEqual((new_adjustment.docstatus, new_adjustment["keys"]), (1, 0))
			self.assertEqual(frappe.db.get_value("Stock Opening Adjustment", adjustment.name, "docstatus"), 2)

			# stored values are engine truth again, corrections carried on the restatement
			last = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": issue.name},
				["stock_value", "stock_value_difference"],
				as_dict=True,
			)
			self.assertAlmostEqual(flt(last.stock_value), 600, places=2)
			self.assertAlmostEqual(flt(last.stock_value_difference), -400, places=2)
			self.assertAlmostEqual(flt(frappe.db.get_value("Bin", key, "stock_value")), 600, places=2)
			self.assertAlmostEqual(stock_account_balance(company, warehouse, name), -37, places=2)

			# unlocked: the period accepts entries again
			make_stock_entry(item_code=item, source=warehouse, qty=1, company=company, posting_date=frontier)
		finally:
			frappe.conf.pop("stock_event_dual_write", None)

	def _frontier(self, company: str, to_date: str):
		closing = submit_closing(company, to_date)
		adjustment = frappe.get_doc(
			doctype="Stock Opening Adjustment", company=company, stock_closing_entry=closing.name
		).insert()
		adjustment.build()
		adjustment.submit()
		return closing, adjustment
