# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, today

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.stock_opening_adjustment.test_stock_opening_adjustment import (
	stock_account_balance,
)
from erpnext.stock.doctype.stock_refold import stock_refold
from erpnext.stock.doctype.warehouse.test_warehouse import create_warehouse
from erpnext.stock.services import stock_fold_refold
from erpnext.tests.utils import ERPNextTestSuite

FLAGS = ("stock_event_dual_write", "stock_fold_authoritative", "stock_fold_gl_adjustment")
COMPANY = "_Test Company with perpetual inventory"


class TestStockFoldRefold(ERPNextTestSuite):
	def test_deep_backdate_is_queued_then_matches_sync_refold(self):
		"""Past REFOLD_CAP a backdate values its own row now, shifts future
		quantities, and queues the rest; once the queue runs, every stored
		value, the Bin and the books equal a synchronous refold's."""
		item = make_item(properties={"is_stock_item": 1}).name
		sync_warehouse = create_warehouse("Refold Sync WH", company=COMPANY)
		queued_warehouse = create_warehouse("Refold Queued WH", company=COMPANY)

		for flag in FLAGS:
			frappe.conf[flag] = 1
		try:
			self._scenario(item, sync_warehouse)

			with (
				patch.object(stock_fold_refold, "REFOLD_CAP", 2),
				patch.object(stock_refold, "enqueue") as enqueue,
			):
				backdated = self._scenario(item, queued_warehouse)
				self.assertTrue(enqueue.called)

			key = {"item_code": item, "warehouse": queued_warehouse}
			row = frappe.db.get_value(
				"Stock Refold", {**key, "status": "Queued"}, ["name", "voucher_no"], as_dict=True
			)
			self.assertEqual(row.voucher_no, backdated)
			own = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": backdated},
				["qty_after_transaction", "stock_value_difference"],
				as_dict=True,
			)
			self.assertEqual(flt(own.qty_after_transaction), 10)
			self.assertAlmostEqual(flt(own.stock_value_difference), 44, places=4)
			self.assertEqual(flt(frappe.db.get_value("Bin", key, "actual_qty")), 8)

			report = stock_refold.process_refold_queue()
		finally:
			for flag in FLAGS:
				frappe.conf.pop(flag, None)

		self.assertEqual(report, {"completed": 1, "failed": 0})
		self.assertEqual(frappe.db.get_value("Stock Refold", row.name, "status"), "Completed")

		sync_rows = self._valuation_rows(item, sync_warehouse)
		queued_rows = self._valuation_rows(item, queued_warehouse)
		self.assertEqual(len(sync_rows), 5)
		for sync, queued in zip(sync_rows, queued_rows, strict=True):
			for field in ("actual_qty", "qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(sync[field], queued[field], places=4, msg=field)

		for warehouse in (sync_warehouse, queued_warehouse):
			stock_value = flt(
				frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "stock_value")
			)
			self.assertAlmostEqual(stock_value, queued_rows[-1].stock_value, places=4)
			self.assertAlmostEqual(stock_account_balance(COMPANY, warehouse), stock_value, places=2)

	def _scenario(self, item: str, warehouse: str) -> str:
		"""Three receipts, an issue, then a receipt backdated between them.
		Returns the backdated voucher."""
		for days, rate in ((-10, 10), (-8, 12), (-6, 14)):
			make_stock_entry(
				item_code=item,
				target=warehouse,
				qty=3,
				basic_rate=rate,
				company=COMPANY,
				posting_date=add_days(today(), days),
			)
		make_stock_entry(
			item_code=item, source=warehouse, qty=5, company=COMPANY, posting_date=add_days(today(), -5)
		)
		return make_stock_entry(
			item_code=item,
			target=warehouse,
			qty=4,
			basic_rate=11,
			company=COMPANY,
			posting_date=add_days(today(), -7),
		).name

	def _valuation_rows(self, item: str, warehouse: str) -> list[frappe._dict]:
		return frappe.get_all(
			"Stock Ledger Entry",
			filters={"item_code": item, "warehouse": warehouse, "is_cancelled": 0},
			fields=["actual_qty", "qty_after_transaction", "stock_value", "stock_value_difference"],
			order_by="posting_datetime, creation, name",
		)
