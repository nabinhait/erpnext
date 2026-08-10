# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import frappe
from frappe.utils import add_days, cint, today

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.stock_reconciliation.test_stock_reconciliation import (
	create_stock_reconciliation,
)
from erpnext.stock.doctype.warehouse.test_warehouse import create_warehouse
from erpnext.stock.services import stock_event_backfill, stock_event_emitter
from erpnext.tests.utils import ERPNextTestSuite

WAREHOUSE = "_Test Warehouse - _TC"


class TestStockEvent(ERPNextTestSuite):
	def test_dual_write_emits_matching_facts(self):
		"""With the flag on, submit emits a Receipt fact whose hash matches the
		SLE, and cancel emits a Reversal referencing it."""
		item = make_item(properties={"is_stock_item": 1}).name

		frappe.conf.stock_event_dual_write = 1
		try:
			stock_entry = make_stock_entry(item_code=item, target=WAREHOUSE, qty=5, rate=100)

			event = frappe.db.get_value(
				"Stock Event",
				{"voucher_no": stock_entry.name},
				["name", "kind", "qty_change", "declared_rate", "content_hash", "sle"],
				as_dict=1,
			)
			self.assertTrue(event)
			self.assertEqual(event.kind, "Receipt")
			self.assertEqual(event.qty_change, 5)
			self.assertEqual(event.declared_rate, 100)

			sle_row = frappe.db.get_value("Stock Ledger Entry", event.sle, "*", as_dict=1)
			self.assertEqual(
				event.content_hash, stock_event_emitter.event_args_from_sle(sle_row)["content_hash"]
			)

			stock_entry.cancel()
			reversal = frappe.db.get_value(
				"Stock Event",
				{"voucher_no": stock_entry.name, "kind": "Reversal"},
				["qty_change", "reverses_event"],
				as_dict=1,
			)
			self.assertTrue(reversal)
			self.assertEqual(reversal.qty_change, -5)
			self.assertEqual(cint(reversal.reverses_event), cint(event.name))
		finally:
			frappe.conf.pop("stock_event_dual_write", None)

	def test_backfill_reproduces_legacy_order(self):
		"""Backfill converts a warehouse's live SLEs (incl. a backdated entry and
		a reconciliation) into facts that pass the order/hash gate, idempotently."""
		item = make_item(properties={"is_stock_item": 1}).name
		warehouse = create_warehouse("Stock Event Backfill WH")

		make_stock_entry(item_code=item, target=warehouse, qty=10, rate=100)
		make_stock_entry(item_code=item, source=warehouse, qty=3)
		make_stock_entry(
			item_code=item, target=warehouse, qty=4, rate=110, posting_date=add_days(today(), -1)
		)
		create_stock_reconciliation(item_code=item, warehouse=warehouse, qty=15, rate=105)

		live_rows = frappe.db.count("Stock Ledger Entry", {"warehouse": warehouse, "is_cancelled": 0})
		summary = stock_event_backfill.run(warehouses=[warehouse])
		self.assertEqual(summary["created"], live_rows)

		report = stock_event_backfill.verify(warehouses=[warehouse])
		self.assertTrue(report["ok"], msg=str(report))
		self.assertEqual(report["checked"], live_rows)

		assertion = frappe.db.get_value(
			"Stock Event",
			{"warehouse": warehouse, "kind": "Assertion"},
			["assert_qty", "assert_rate"],
			as_dict=1,
		)
		self.assertTrue(assertion)
		self.assertEqual(assertion.assert_qty, 15)

		rerun = stock_event_backfill.run(warehouses=[warehouse])
		self.assertEqual(rerun["created"], 0)
		self.assertEqual(rerun["skipped"], live_rows)
