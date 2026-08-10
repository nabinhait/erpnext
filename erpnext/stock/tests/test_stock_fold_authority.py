# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import json

import frappe

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.warehouse.test_warehouse import create_warehouse
from erpnext.tests.utils import ERPNextTestSuite


class TestStockFoldAuthority(ERPNextTestSuite):
	def test_fold_parity_with_legacy(self):
		"""An identical FIFO scenario — layered receipts, cross-layer issues, a
		cancellation — must produce byte-equivalent valuation whether the legacy
		engine or the fold values it."""
		item = make_item(properties={"is_stock_item": 1}).name
		legacy_warehouse = create_warehouse("Fold Parity Legacy WH")
		fold_warehouse = create_warehouse("Fold Parity Fold WH")

		frappe.conf.stock_event_dual_write = 1
		try:
			self._run_scenario(item, legacy_warehouse)

			frappe.conf.stock_fold_authoritative = 1
			self._run_scenario(item, fold_warehouse)
		finally:
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		legacy_rows = self._valuation_rows(item, legacy_warehouse)
		fold_rows = self._valuation_rows(item, fold_warehouse)
		self.assertEqual(len(legacy_rows), len(fold_rows))

		for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
			for field in ("actual_qty", "qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=field)
			self.assertAlmostEqual(legacy.valuation_rate, fold.valuation_rate, places=4)
			self._assert_queue_equal(legacy.stock_queue, fold.stock_queue)

		legacy_bin = self._bin(item, legacy_warehouse)
		fold_bin = self._bin(item, fold_warehouse)
		self.assertAlmostEqual(legacy_bin.actual_qty, fold_bin.actual_qty, places=4)
		self.assertAlmostEqual(legacy_bin.stock_value, fold_bin.stock_value, places=4)

		# the fold path must actually have run: a checkpoint exists only for the fold key
		self.assertTrue(
			frappe.db.exists("Stock Fold State", {"item_code": item, "warehouse": fold_warehouse})
		)
		self.assertFalse(
			frappe.db.exists("Stock Fold State", {"item_code": item, "warehouse": legacy_warehouse})
		)

	def _run_scenario(self, item: str, warehouse: str) -> None:
		make_stock_entry(item_code=item, target=warehouse, qty=10, rate=100)
		make_stock_entry(item_code=item, target=warehouse, qty=5, rate=120)
		make_stock_entry(item_code=item, source=warehouse, qty=8)
		cancelled = make_stock_entry(item_code=item, target=warehouse, qty=4, rate=90)
		cancelled.cancel()
		make_stock_entry(item_code=item, source=warehouse, qty=3)

	def _valuation_rows(self, item: str, warehouse: str) -> list[frappe._dict]:
		return frappe.get_all(
			"Stock Ledger Entry",
			filters={"item_code": item, "warehouse": warehouse, "is_cancelled": 0},
			fields=[
				"actual_qty",
				"qty_after_transaction",
				"stock_value",
				"stock_value_difference",
				"valuation_rate",
				"stock_queue",
			],
			order_by="posting_datetime, creation, name",
		)

	def _bin(self, item: str, warehouse: str) -> frappe._dict:
		return frappe.db.get_value(
			"Bin", {"item_code": item, "warehouse": warehouse}, ["actual_qty", "stock_value"], as_dict=1
		)

	def _assert_queue_equal(self, legacy_queue: str, fold_queue: str) -> None:
		legacy = json.loads(legacy_queue or "[]")
		fold = json.loads(fold_queue or "[]")
		self.assertEqual(len(legacy), len(fold), msg=f"{legacy} vs {fold}")
		for legacy_layer, fold_layer in zip(legacy, fold, strict=True):
			self.assertAlmostEqual(legacy_layer[0], fold_layer[0], places=4)
			self.assertAlmostEqual(legacy_layer[1], fold_layer[1], places=4)
