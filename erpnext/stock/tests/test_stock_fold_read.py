# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.warehouse.test_warehouse import create_warehouse
from erpnext.stock.services import stock_fold_read
from erpnext.tests.utils import ERPNextTestSuite


class TestStockFoldRead(ERPNextTestSuite):
	def test_checkpoint_resume_equals_full_replay(self):
		"""A read from the nearest checkpoint plus the folded tail must equal a
		fold of the whole history — and ledger rows carry correct running values."""
		item = make_item(properties={"is_stock_item": 1}).name
		warehouse = create_warehouse("Fold Read WH")

		frappe.conf.stock_event_dual_write = 1
		try:
			make_stock_entry(
				item_code=item, target=warehouse, qty=10, rate=100, posting_date=add_days(today(), -5)
			)
			make_stock_entry(item_code=item, source=warehouse, qty=3, posting_date=add_days(today(), -4))

			closing = frappe.get_doc(
				doctype="Stock Closing Entry",
				company="_Test Company",
				from_date=add_days(today(), -30),
				to_date=add_days(today(), -3),
			)
			with patch("erpnext.stock.doctype.stock_closing_entry.stock_closing_entry.enqueue"):
				closing.submit()
			closing.create_stock_closing_balance_entries()

			self.assertTrue(
				frappe.db.exists("Stock Fold Checkpoint", {"item_code": item, "warehouse": warehouse})
			)

			checkpoint_state = stock_fold_read.state_as_of(
				item, warehouse, add_days(today(), -3) + " 23:59:59"
			)
			self.assertAlmostEqual(checkpoint_state.qty, 7, places=4)
			self.assertAlmostEqual(checkpoint_state.value, 700, places=4)

			make_stock_entry(
				item_code=item, target=warehouse, qty=5, rate=120, posting_date=add_days(today(), -2)
			)
			make_stock_entry(item_code=item, source=warehouse, qty=4)

			# resumes from the checkpoint (tail = 2 events), must equal full fold:
			# FIFO leaves 3 @ 100 + 5 @ 120
			state = stock_fold_read.state_as_of(item, warehouse, today() + " 23:59:59")
			self.assertAlmostEqual(state.qty, 8, places=4)
			self.assertAlmostEqual(state.value, 900, places=4)

			rows = stock_fold_read.ledger_rows(
				item, warehouse, add_days(today(), -3) + " 23:59:59", today() + " 23:59:59"
			)
			self.assertEqual([row["qty_after"] for row in rows], [12, 8])
			self.assertEqual([row["value_after"] for row in rows], [1300, 900])

			# cancelling the closing removes its checkpoints; reads still work from zero
			closing.reload()
			closing.cancel()
			self.assertFalse(
				frappe.db.exists("Stock Fold Checkpoint", {"item_code": item, "warehouse": warehouse})
			)
			state = stock_fold_read.state_as_of(item, warehouse, today() + " 23:59:59")
			self.assertAlmostEqual(state.value, 900, places=4)
		finally:
			frappe.conf.pop("stock_event_dual_write", None)

	def test_stock_balance_report_parity(self):
		"""The fold-based Stock Balance must agree with the legacy report on a
		clean scenario, opening balances included."""
		from erpnext.stock.services import report_parity

		item = make_item(properties={"is_stock_item": 1}).name
		warehouse = create_warehouse("Fold Balance WH")

		frappe.conf.stock_event_dual_write = 1
		try:
			make_stock_entry(
				item_code=item, target=warehouse, qty=10, rate=100, posting_date=add_days(today(), -10)
			)
			make_stock_entry(item_code=item, source=warehouse, qty=2, posting_date=add_days(today(), -6))
			make_stock_entry(
				item_code=item, target=warehouse, qty=5, rate=120, posting_date=add_days(today(), -2)
			)
			make_stock_entry(item_code=item, source=warehouse, qty=4)

			report = report_parity.stock_balance(
				"_Test Company", add_days(today(), -7), today(), warehouse=warehouse
			)
		finally:
			frappe.conf.pop("stock_event_dual_write", None)

		self.assertEqual(report["compared"], 1)
		self.assertEqual(report["qty_matched"], 1)
		self.assertEqual(report["value_matched"], 1, msg=str(report))
		self.assertFalse(report["only_legacy_count"] or report["only_fold_count"])

	def test_ledger_and_ageing_report_parity(self):
		"""Fold ledger rows match stored values; fold ageing ages layers from
		their source events."""
		from erpnext.stock.services import report_parity

		item = make_item(properties={"is_stock_item": 1}).name
		warehouse = create_warehouse("Fold Ledger WH")

		frappe.conf.stock_event_dual_write = 1
		frappe.conf.stock_fold_authoritative = 1
		try:
			make_stock_entry(
				item_code=item, target=warehouse, qty=10, rate=100, posting_date=add_days(today(), -40)
			)
			make_stock_entry(item_code=item, source=warehouse, qty=4, posting_date=add_days(today(), -20))
			make_stock_entry(
				item_code=item, target=warehouse, qty=6, rate=120, posting_date=add_days(today(), -5)
			)

			ledger = report_parity.stock_ledger(
				"_Test Company", add_days(today(), -60), today(), warehouse=warehouse
			)
			ageing = report_parity.stock_ageing("_Test Company", today(), warehouse=warehouse)
		finally:
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		self.assertEqual(ledger["rows"], 3)
		self.assertEqual(ledger["qty_matched"], 3)
		self.assertEqual(ledger["value_matched"], 3, msg=str(ledger))

		self.assertEqual(ageing["compared"], 1)
		self.assertEqual(ageing["qty_matched"], 1)
		self.assertEqual(ageing["age_within_5d"], 1, msg=str(ageing))
