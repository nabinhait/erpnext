# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import json

import frappe
from frappe.utils import add_days, flt, today

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.stock_reconciliation.test_stock_reconciliation import (
	create_stock_reconciliation,
)
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

	def test_fold_parity_including_reco_and_backdate(self):
		"""Reconciliations fold as assertions; a mid-history backdate refolds the
		key synchronously. Legacy needs its background repost processed to reach
		the same values — the fold side must be right without one."""
		company = "_Test Company with perpetual inventory"
		item = make_item(properties={"is_stock_item": 1}).name
		legacy_warehouse = create_warehouse("Fold BD Legacy WH", company=company)
		fold_warehouse = create_warehouse("Fold BD Fold WH", company=company)

		frappe.conf.stock_event_dual_write = 1
		try:
			legacy_vouchers = self._run_backdate_scenario(item, legacy_warehouse, company)
			self._process_pending_reposts()

			frappe.conf.stock_fold_authoritative = 1
			fold_vouchers = self._run_backdate_scenario(item, fold_warehouse, company)

			# the backdated voucher's own GL is posted from fold-computed svd at
			# submit — correct before any repost runs
			backdated = fold_vouchers[-2]
			svd = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": backdated, "is_cancelled": 0},
				"stock_value_difference",
			)
			gl_debit = sum(row.debit for row in self._gl_rows(backdated))
			self.assertAlmostEqual(gl_debit, svd, places=4)

			# GL corrections for previously posted vouchers still ride the
			# coexisting repost; process both sides, then GL must match exactly
			self._process_pending_reposts()
		finally:
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		for legacy_voucher, fold_voucher in zip(legacy_vouchers, fold_vouchers, strict=True):
			self.assertEqual(
				self._gl_totals(legacy_voucher, legacy_warehouse),
				self._gl_totals(fold_voucher, fold_warehouse),
				msg=fold_voucher,
			)

		legacy_rows = self._valuation_rows(item, legacy_warehouse)
		fold_rows = self._valuation_rows(item, fold_warehouse)
		self.assertEqual(len(legacy_rows), len(fold_rows))

		for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
			for field in ("actual_qty", "qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=field)

		legacy_bin = self._bin(item, legacy_warehouse)
		fold_bin = self._bin(item, fold_warehouse)
		self.assertAlmostEqual(legacy_bin.actual_qty, fold_bin.actual_qty, places=4)
		self.assertAlmostEqual(legacy_bin.stock_value, fold_bin.stock_value, places=4)

	def test_suppressed_legacy_repost(self):
		"""With repost suppression on, a fold-covered backdate scenario creates no
		Repost Item Valuation at all — values and GL are correct synchronously and
		match a legacy run that needed its background reposts."""
		company = "_Test Company with perpetual inventory"
		item = make_item(properties={"is_stock_item": 1}).name
		legacy_warehouse = create_warehouse("No Repost Legacy WH", company=company)
		fold_warehouse = create_warehouse("No Repost Fold WH", company=company)

		frappe.conf.stock_event_dual_write = 1
		try:
			legacy_vouchers = self._run_backdate_scenario(item, legacy_warehouse, company)
			self._process_pending_reposts()

			frappe.conf.stock_fold_authoritative = 1
			frappe.conf.stock_fold_suppress_legacy_repost = 1
			fold_vouchers = self._run_backdate_scenario(item, fold_warehouse, company)
		finally:
			frappe.conf.pop("stock_fold_suppress_legacy_repost", None)
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		self.assertFalse(
			frappe.get_all("Repost Item Valuation", filters={"voucher_no": ("in", fold_vouchers)})
		)

		for legacy_voucher, fold_voucher in zip(legacy_vouchers, fold_vouchers, strict=True):
			self.assertEqual(
				self._gl_totals(legacy_voucher, legacy_warehouse),
				self._gl_totals(fold_voucher, fold_warehouse),
				msg=fold_voucher,
			)

		legacy_rows = self._valuation_rows(item, legacy_warehouse)
		fold_rows = self._valuation_rows(item, fold_warehouse)
		for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
			for field in ("qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=field)

	def test_append_only_gl_adjustment(self):
		"""With stock_fold_gl_adjustment, a backdate never rewrites posted GL: the
		net delta posts as fresh rows on the backdated voucher, and total account
		balances still equal a legacy run whose GL was rewritten by reposts."""
		company = "_Test Company with perpetual inventory"
		item = make_item(properties={"is_stock_item": 1}).name
		legacy_warehouse = create_warehouse("GL Adj Legacy WH", company=company)
		fold_warehouse = create_warehouse("GL Adj Fold WH", company=company)

		frappe.conf.stock_event_dual_write = 1
		try:
			legacy_vouchers = self._run_backdate_scenario(item, legacy_warehouse, company)
			self._process_pending_reposts()

			frappe.conf.stock_fold_authoritative = 1
			frappe.conf.stock_fold_gl_adjustment = 1
			fold_vouchers = self._run_backdate_scenario(item, fold_warehouse, company)
		finally:
			frappe.conf.pop("stock_fold_gl_adjustment", None)
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		# no legacy repost machinery engaged
		self.assertFalse(
			frappe.get_all("Repost Item Valuation", filters={"voucher_no": ("in", fold_vouchers)})
		)

		# the backdated voucher carries the adjustment rows
		adjustment_rows = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": fold_vouchers[-2], "remarks": ("like", "Stock value adjustment%")},
		)
		self.assertTrue(adjustment_rows)

		# books net out identically even though no historical GL was rewritten
		self.assertEqual(
			self._account_balances(legacy_vouchers, legacy_warehouse),
			self._account_balances(fold_vouchers, fold_warehouse),
		)

		# and the stock ledger itself still matches legacy exactly
		legacy_rows = self._valuation_rows(item, legacy_warehouse)
		fold_rows = self._valuation_rows(item, fold_warehouse)
		for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
			for field in ("qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=field)

		# per-voucher dating: stock value and stock account balance agree on
		# EVERY as-of date, not only at the end
		self._assert_stock_account_sync(item, fold_warehouse, fold_vouchers)

	def _assert_stock_account_sync(self, item: str, warehouse: str, vouchers: list[str]) -> None:
		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"item_code": item, "warehouse": warehouse, "is_cancelled": 0},
			fields=["posting_date", "stock_value"],
			order_by="posting_datetime, creation, name",
		)
		gl_rows = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": ("in", vouchers), "account": warehouse, "is_cancelled": 0},
			fields=["posting_date", "debit", "credit"],
		)
		for check_date in sorted({sle.posting_date for sle in sles}):
			expected = [sle.stock_value for sle in sles if sle.posting_date <= check_date][-1]
			balance = sum(row.debit - row.credit for row in gl_rows if row.posting_date <= check_date)
			self.assertAlmostEqual(balance, expected, places=4, msg=str(check_date))

	def test_lot_parity_with_legacy(self):
		"""Batch-tracked flows fold per lot (moving average per batch) and must
		match legacy batch-wise valuation on the hot path."""
		item = make_item(
			properties={
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": "FOLDBAT-.#####",
			}
		).name
		legacy_warehouse = create_warehouse("Lot Parity Legacy WH")
		fold_warehouse = create_warehouse("Lot Parity Fold WH")

		previous = frappe.db.get_single_value(
			"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"
		)
		frappe.db.set_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward", 1)
		frappe.conf.stock_event_dual_write = 1
		try:
			self._run_lot_scenario(item, legacy_warehouse)

			frappe.conf.stock_fold_authoritative = 1
			self._run_lot_scenario(item, fold_warehouse)
		finally:
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)
			frappe.db.set_single_value(
				"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward", previous
			)

		legacy_rows = self._valuation_rows(item, legacy_warehouse)
		fold_rows = self._valuation_rows(item, fold_warehouse)
		self.assertEqual(len(legacy_rows), len(fold_rows))

		for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
			for field in ("actual_qty", "qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=field)

		legacy_bin = self._bin(item, legacy_warehouse)
		fold_bin = self._bin(item, fold_warehouse)
		self.assertAlmostEqual(legacy_bin.actual_qty, fold_bin.actual_qty, places=4)
		self.assertAlmostEqual(legacy_bin.stock_value, fold_bin.stock_value, places=4)

		self.assertTrue(
			frappe.db.exists("Stock Fold State", {"item_code": item, "warehouse": fold_warehouse})
		)

	def _run_lot_scenario(self, item: str, warehouse: str) -> None:
		make_stock_entry(item_code=item, target=warehouse, qty=10, rate=100)
		make_stock_entry(item_code=item, target=warehouse, qty=5, rate=120)
		make_stock_entry(item_code=item, source=warehouse, qty=8)
		make_stock_entry(item_code=item, target=warehouse, qty=4, rate=90)
		make_stock_entry(item_code=item, source=warehouse, qty=3)

	def _account_balances(self, vouchers: list[str], warehouse: str) -> dict[str, float]:
		balances: dict[str, float] = {}
		for voucher in vouchers:
			for account, (debit, credit) in self._gl_totals(voucher, warehouse).items():
				balances[account] = round(balances.get(account, 0.0) + debit - credit, 4)
		return balances

	def test_company_scoping(self):
		"""With a company allow-list that excludes the transaction's company, the
		fold path must stand aside entirely."""
		item = make_item(properties={"is_stock_item": 1}).name
		warehouse = create_warehouse("Fold Company Scope WH")

		frappe.conf.stock_event_dual_write = 1
		frappe.conf.stock_fold_authoritative = 1
		frappe.conf.stock_fold_authoritative_companies = ["Some Other Company"]
		try:
			make_stock_entry(item_code=item, target=warehouse, qty=2, rate=50)
		finally:
			frappe.conf.pop("stock_fold_authoritative_companies", None)
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		self.assertFalse(frappe.db.exists("Stock Fold State", {"item_code": item, "warehouse": warehouse}))

	def test_mixed_batch_pools_never_borrow(self):
		"""One key, two batches: a batchwise batch spends its own money, a
		quantity-tag batch (flag off) costs the shared pool's rate, and full
		stock-out closes at exactly zero — no residual value at zero qty."""
		company = "_Test Company with perpetual inventory"
		item = make_item(
			properties={"is_stock_item": 1, "has_batch_no": 1, "valuation_method": "Moving Average"}
		).name
		warehouse = create_warehouse("Mixed Batch Pool WH", company=company)

		batches = {}
		for batch_id, flagged in (("OLD", 0), ("NEW", 1)):
			batch = frappe.get_doc(
				{"doctype": "Batch", "item": item, "batch_id": f"{item}-{batch_id}"}
			).insert()
			if not flagged:
				batch.db_set("use_batchwise_valuation", 0)
				frappe.clear_document_cache("Batch", batch.name)
			batches[batch_id] = batch.name

		previous = frappe.db.get_single_value(
			"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"
		)
		frappe.db.set_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward", 1)
		frappe.conf.stock_event_dual_write = 1
		frappe.conf.stock_fold_authoritative = 1
		try:
			moves = [
				("OLD", 10, 50, None),
				("NEW", 10, 70, None),
				("NEW", -2, None, -140),
				("OLD", -2, None, -100),  # pool rate 50, not the 58.89 whole-position blend
				("OLD", -8, None, -400),
				("NEW", -8, None, -560),
			]
			for batch_id, qty, rate, expected_difference in moves:
				entry = make_stock_entry(
					item_code=item,
					target=warehouse if qty > 0 else None,
					source=warehouse if qty < 0 else None,
					qty=abs(qty),
					basic_rate=rate,
					batch_no=batches[batch_id],
					company=company,
				)
				if expected_difference is None:
					continue
				difference = frappe.db.get_value(
					"Stock Ledger Entry",
					{"voucher_no": entry.name, "is_cancelled": 0},
					"stock_value_difference",
				)
				self.assertAlmostEqual(flt(difference), expected_difference, places=2, msg=entry.name)

			final = frappe.db.get_value(
				"Stock Ledger Entry",
				{"item_code": item, "warehouse": warehouse, "is_cancelled": 0},
				["qty_after_transaction", "stock_value"],
				order_by="posting_datetime desc, creation desc",
				as_dict=1,
			)
			self.assertEqual(flt(final.qty_after_transaction), 0)
			self.assertAlmostEqual(flt(final.stock_value), 0, places=2)

			# semantics are fixed at birth: the flag is set_only_once, so the
			# save path rejects any post-insert flip (fold correctness depends
			# on this — a flip would strand the batch's sub-fold money)
			locked = frappe.get_doc("Batch", batches["NEW"])
			locked.use_batchwise_valuation = 0
			self.assertRaises(frappe.CannotChangeConstantError, locked.save)
		finally:
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)
			frappe.db.set_single_value(
				"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward", previous
			)

	def test_lot_landed_cost_as_revaluation(self):
		"""Batch- and serial-tracked LCVs take the fold path too: allocations fold
		as lot sub-states, the revaluation uplifts the lots, and the books net
		identically to legacy's rewrite — with difference entries and no RIVs."""
		from erpnext.stock.doctype.landed_cost_voucher.test_landed_cost_voucher import (
			create_landed_cost_voucher,
		)
		from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt

		company = "_Test Company with perpetual inventory"
		tracked_items = {
			"batch": make_item(
				properties={
					"is_stock_item": 1,
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "FOLDLCV-.#####",
				}
			).name,
			"serial": make_item(
				properties={
					"is_stock_item": 1,
					"has_serial_no": 1,
					"serial_no_series": "FOLDSN-.#####",
				}
			).name,
		}

		previous = frappe.db.get_single_value(
			"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"
		)
		frappe.db.set_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward", 1)
		frappe.conf.stock_event_dual_write = 1
		try:
			results = {}
			for label, item in tracked_items.items():
				legacy_warehouse = create_warehouse(f"LCV Lot Legacy {label} WH", company=company)
				fold_warehouse = create_warehouse(f"LCV Lot Fold {label} WH", company=company)

				def scenario(warehouse, item=item):
					receipt = make_purchase_receipt(
						item_code=item, qty=10, rate=100, warehouse=warehouse, company=company
					)
					issue = make_stock_entry(item_code=item, source=warehouse, qty=4, company=company)
					lcv = create_landed_cost_voucher("Purchase Receipt", receipt.name, company, charges=200)
					return [receipt.name, issue.name, lcv.name]

				frappe.conf.pop("stock_fold_authoritative", None)
				frappe.conf.pop("stock_fold_gl_adjustment", None)
				legacy_vouchers = scenario(legacy_warehouse)
				self._process_pending_reposts()

				frappe.conf.stock_fold_authoritative = 1
				frappe.conf.stock_fold_gl_adjustment = 1
				fold_vouchers = scenario(fold_warehouse)
				results[label] = (legacy_warehouse, fold_warehouse, legacy_vouchers, fold_vouchers, item)
		finally:
			frappe.conf.pop("stock_fold_gl_adjustment", None)
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)
			frappe.db.set_single_value(
				"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward", previous
			)

		for label, (legacy_warehouse, fold_warehouse, legacy_vouchers, fold_vouchers, item) in results.items():
			self.assertFalse(
				frappe.get_all("Repost Item Valuation", filters={"voucher_no": ("in", fold_vouchers)}),
				msg=label,
			)
			self.assertTrue(
				frappe.db.exists("Stock Event", {"kind": "Revaluation", "voucher_no": fold_vouchers[2]}),
				msg=label,
			)

			legacy_rows = self._valuation_rows(item, legacy_warehouse)
			fold_rows = self._valuation_rows(item, fold_warehouse)
			self.assertEqual(len(legacy_rows), len(fold_rows), msg=label)
			for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
				for field in ("qty_after_transaction", "stock_value", "stock_value_difference"):
					self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=f"{label}:{field}")

			self.assertEqual(
				self._account_balances(legacy_vouchers, legacy_warehouse),
				self._account_balances(fold_vouchers, fold_warehouse),
				msg=label,
			)

	def _run_backdate_scenario(self, item: str, warehouse: str, company: str) -> list[str]:
		vouchers = [
			make_stock_entry(
				item_code=item,
				target=warehouse,
				qty=10,
				rate=100,
				posting_date=add_days(today(), -5),
				company=company,
			),
			make_stock_entry(
				item_code=item,
				target=warehouse,
				qty=5,
				rate=120,
				posting_date=add_days(today(), -3),
				company=company,
			),
			make_stock_entry(
				item_code=item, source=warehouse, qty=6, posting_date=add_days(today(), -2), company=company
			),
			create_stock_reconciliation(
				item_code=item,
				warehouse=warehouse,
				qty=12,
				rate=110,
				posting_date=add_days(today(), -1),
				company=company,
			),
			make_stock_entry(item_code=item, source=warehouse, qty=4, company=company),
			# backdated between the first two receipts: refolds up to the reconciliation
			make_stock_entry(
				item_code=item,
				target=warehouse,
				qty=3,
				rate=90,
				posting_date=add_days(today(), -4),
				company=company,
			),
			make_stock_entry(item_code=item, source=warehouse, qty=2, company=company),
		]
		return [voucher.name for voucher in vouchers]

	def _gl_rows(self, voucher_no: str) -> list[frappe._dict]:
		return frappe.get_all(
			"GL Entry",
			filters={"voucher_no": voucher_no, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)

	def _gl_totals(self, voucher_no: str, warehouse: str) -> dict[str, tuple[float, float]]:
		"""GL amounts per account, with the warehouse's own account normalized so
		two warehouses' postings are comparable."""
		totals: dict[str, tuple[float, float]] = {}
		for row in self._gl_rows(voucher_no):
			account = "WAREHOUSE" if row.account == warehouse else row.account
			debit, credit = totals.get(account, (0.0, 0.0))
			totals[account] = (round(debit + row.debit, 4), round(credit + row.credit, 4))
		return totals

	def _process_pending_reposts(self) -> None:
		from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

		pending = frappe.get_all(
			"Repost Item Valuation",
			filters={"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
			pluck="name",
		)
		for name in pending:
			repost(frappe.get_doc("Repost Item Valuation", name))

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

	def test_landed_cost_as_revaluation(self):
		"""An LCV under fold authority posts append-only revaluation GL and
		trues up downstream consumption — netting the same books as legacy's
		cancel/recreate, with no Repost Item Valuation."""
		from erpnext.stock.doctype.landed_cost_voucher.test_landed_cost_voucher import (
			create_landed_cost_voucher,
		)
		from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import make_purchase_receipt

		company = "_Test Company with perpetual inventory"
		item = make_item(properties={"is_stock_item": 1}).name
		legacy_warehouse = create_warehouse("LCV Legacy WH", company=company)
		fold_warehouse = create_warehouse("LCV Fold WH", company=company)

		def scenario(warehouse):
			receipt = make_purchase_receipt(
				item_code=item, qty=10, rate=100, warehouse=warehouse, company=company
			)
			issue = make_stock_entry(item_code=item, source=warehouse, qty=4, company=company)
			lcv = create_landed_cost_voucher("Purchase Receipt", receipt.name, company, charges=200)
			return [receipt.name, issue.name, lcv.name]

		frappe.conf.stock_event_dual_write = 1
		try:
			legacy_vouchers = scenario(legacy_warehouse)
			self._process_pending_reposts()

			frappe.conf.stock_fold_authoritative = 1
			frappe.conf.stock_fold_gl_adjustment = 1
			fold_vouchers = scenario(fold_warehouse)
		finally:
			frappe.conf.pop("stock_fold_gl_adjustment", None)
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)

		# no repost machinery on the fold side
		self.assertFalse(
			frappe.get_all("Repost Item Valuation", filters={"voucher_no": ("in", fold_vouchers)})
		)
		riv_item = frappe.get_all(
			"Repost Item Valuation", filters={"item_code": item, "warehouse": fold_warehouse}
		)
		self.assertFalse(riv_item)

		# the revaluation fact exists and the LCV carries its GL
		self.assertTrue(
			frappe.db.exists(
				"Stock Event", {"kind": "Revaluation", "voucher_no": fold_vouchers[2]}
			)
		)
		self.assertTrue(
			frappe.get_all("GL Entry", filters={"voucher_no": fold_vouchers[2], "is_cancelled": 0})
		)
		self.assertTrue(
			frappe.get_all(
				"GL Entry",
				filters={
					"voucher_no": fold_vouchers[2],
					"is_cancelled": 0,
					"remarks": ("like", "%landed cost%"),
				},
			)
		)
		# downstream corrections never land on other vouchers
		for voucher in fold_vouchers[:2]:
			self.assertFalse(
				frappe.get_all(
					"GL Entry",
					filters={"voucher_no": voucher, "remarks": ("like", "Stock value adjustment%")},
				),
				msg=voucher,
			)

		# ledger values identical to legacy-after-repost
		legacy_rows = self._valuation_rows(item, legacy_warehouse)
		fold_rows = self._valuation_rows(item, fold_warehouse)
		self.assertEqual(len(legacy_rows), len(fold_rows))
		for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
			for field in ("qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=field)

		# cancel mirrors: negative fact + LCV's own GL reversed, nothing new posted
		frappe.conf.stock_event_dual_write = 1
		frappe.conf.stock_fold_authoritative = 1
		frappe.conf.stock_fold_gl_adjustment = 1
		try:
			frappe.get_doc("Landed Cost Voucher", fold_vouchers[2]).cancel()
		finally:
			frappe.conf.pop("stock_fold_gl_adjustment", None)
			frappe.conf.pop("stock_fold_authoritative", None)
			frappe.conf.pop("stock_event_dual_write", None)
		frappe.get_doc("Landed Cost Voucher", legacy_vouchers[2]).cancel()
		self._process_pending_reposts()

		revaluations = frappe.get_all(
			"Stock Event",
			filters={"kind": "Revaluation", "voucher_no": fold_vouchers[2]},
			fields=["value_change"],
		)
		self.assertEqual(len(revaluations), 2)
		self.assertAlmostEqual(sum(row.value_change for row in revaluations), 0, places=4)
		self.assertFalse(
			frappe.get_all("GL Entry", filters={"voucher_no": fold_vouchers[2], "is_cancelled": 0})
		)
		legacy_rows = self._valuation_rows(item, legacy_warehouse)
		fold_rows = self._valuation_rows(item, fold_warehouse)
		self.assertEqual(len(legacy_rows), len(fold_rows))
		for legacy, fold in zip(legacy_rows, fold_rows, strict=True):
			for field in ("qty_after_transaction", "stock_value", "stock_value_difference"):
				self.assertAlmostEqual(legacy[field], fold[field], places=4, msg=field)

		# and the books net out the same
		self.assertEqual(
			self._account_balances(legacy_vouchers, legacy_warehouse),
			self._account_balances(fold_vouchers, fold_warehouse),
		)
