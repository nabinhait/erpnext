# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Opening Adjustment — the frontier between the frozen legacy era and
the engine era (design doc Part 4, "The v17 cutover").

Owned by a submitted Stock Closing Entry. Computing it folds every key of
the company to the closing instant and lists engine truth next to legacy's
stored balance; the full per-key result is attached to the document, the
keys that differ are shown in the table. Submitting it pins every key with a
baseline Assertion at engine values (batch sub-states seeded from the fold,
negative balances as exposure), books the net value difference per stock
account against the adjustment account on the first open day, and shifts
Bins by the same deltas. Cancelling the closing entry reopens the year and
cancels the adjustment with it; on its own it cannot be cancelled while the
closing stands, because that would strand the books at legacy's balance.
"""

import gzip
import json

import frappe
from frappe import _
from frappe.desk.form.load import get_attachments
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, get_link_to_form
from frappe.utils.background_jobs import enqueue

from erpnext.stock.services import stock_engine_bridge, stock_fold_cutover

QTY_TOLERANCE = 1e-6


class StockOpeningAdjustment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.stock.doctype.stock_opening_adjustment_item.stock_opening_adjustment_item import (
			StockOpeningAdjustmentItem,
		)

		adjustment_account: DF.Link | None
		amended_from: DF.Link | None
		company: DF.Link
		items: DF.Table[StockOpeningAdjustmentItem]
		keys: DF.Int
		moment: DF.Datetime | None
		naming_series: DF.Literal["SOA-.YYYY.-.#####"]
		posting_date: DF.Date | None
		skipped_keys: DF.Int
		status: DF.Literal["Draft", "Queued", "Ready", "Failed", "Submitted", "Cancelled"]
		stock_closing_entry: DF.Link
		threshold: DF.Currency
		total_delta: DF.Currency
		within_threshold: DF.Check
	# end: auto-generated types

	def validate(self):
		closing = self.closing_entry()
		self.validate_closing(closing)
		self.moment = stock_engine_bridge.end_of_day(closing.to_date)
		self.posting_date = add_days(closing.to_date, 1)
		if not self.adjustment_account:
			self.adjustment_account = frappe.get_cached_value(
				"Company", self.company, "stock_adjustment_account"
			)

	def closing_entry(self) -> frappe._dict:
		return frappe.db.get_value(
			"Stock Closing Entry",
			self.stock_closing_entry,
			["company", "to_date", "docstatus"],
			as_dict=True,
		)

	def validate_closing(self, closing: frappe._dict) -> None:
		if closing.company != self.company:
			frappe.throw(
				_("Stock Closing Entry {0} belongs to another company").format(self.stock_closing_entry)
			)
		if closing.docstatus != 1:
			frappe.throw(
				_("Submit Stock Closing Entry {0} first: it is the lock this adjustment sits on").format(
					get_link_to_form("Stock Closing Entry", self.stock_closing_entry)
				)
			)
		live = frappe.db.exists(
			"Stock Opening Adjustment",
			{"stock_closing_entry": self.stock_closing_entry, "docstatus": 1, "name": ("!=", self.name)},
		)
		if live:
			frappe.throw(
				_("{0} is already the live opening adjustment for this closing").format(
					get_link_to_form("Stock Opening Adjustment", live)
				)
			)

	def before_submit(self):
		if self.status != "Ready":
			frappe.throw(_("Compute the adjustment before submitting it"))
		if flt(self.total_delta) and not self.adjustment_account:
			frappe.throw(_("Adjustment Account is required to book the value delta"))

	def on_submit(self):
		rows = self.prepared_rows()
		stock_fold_cutover.emit_baselines(
			self.company, self.moment, (_baseline(row) for row in rows), owner=(self.doctype, self.name)
		)
		self.post_gl_entries(rows)
		self.shift_bins(rows, direction=1)
		self.db_set("status", "Submitted")

	def before_cancel(self):
		self.validate_reopen()

	def on_cancel(self):
		from erpnext.accounts.general_ledger import make_reverse_gl_entries

		self.ignore_linked_doctypes = ("GL Entry", "Stock Event")
		make_reverse_gl_entries(voucher_type=self.doctype, voucher_no=self.name)
		self.shift_bins(self.prepared_rows(), direction=-1)
		stock_fold_cutover.invalidate_fold_state(self.company)
		self.db_set("status", "Cancelled")

	def validate_reopen(self) -> None:
		"""Only the owning closing may take this down: when it cascades, its
		own cancel is already written."""
		if self.closing_entry().docstatus != 1:
			return
		frappe.throw(
			_(
				"Cancel Stock Closing Entry {0} to reopen the period; this adjustment is cancelled along with it"
			).format(get_link_to_form("Stock Closing Entry", self.stock_closing_entry)),
			title=_("Frontier Locked"),
		)

	@frappe.whitelist(methods=["POST"])
	def compute(self):
		self.check_permission("write")
		self.db_set("status", "Queued")
		enqueue(compute_opening_adjustment, name=self.name, queue="long", timeout=3600)
		frappe.msgprint(
			_("Folding every key of {0} to the frontier; the breakdown appears when it is done.").format(
				self.company
			)
		)

	def build(self) -> None:
		"""Fold the company to the frontier and record engine truth against
		legacy: the full result as an attachment, the differing keys in the
		table, the totals on the document."""
		rows = stock_fold_cutover.opening_delta(self.company, self.moment)
		pinned = [row for row in rows if not row.get("skipped")]
		self.keys = len(pinned)
		self.skipped_keys = len(rows) - len(pinned)
		self.total_delta = flt(sum(row.delta for row in pinned), 2)
		self.threshold = flt(frappe.db.get_single_value("Stock Settings", "opening_adjustment_threshold"))
		self.within_threshold = cint(self.threshold > 0 and abs(self.total_delta) <= self.threshold)
		self.set("items", [_item_row(row) for row in pinned if _differs(row)])
		self.status = "Ready"
		self.save()
		self.attach_prepared(pinned)

	def attach_prepared(self, rows: list[frappe._dict]) -> None:
		for attachment in get_attachments(self.doctype, self.name):
			frappe.delete_doc("File", attachment.name, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{frappe.scrub(self.name)}-baselines.json.gz",
				"attached_to_doctype": self.doctype,
				"attached_to_name": self.name,
				"content": gzip.compress(frappe.safe_encode(frappe.as_json(rows))),
				"is_private": 1,
			}
		).save(ignore_permissions=True)

	def prepared_rows(self) -> list[frappe._dict]:
		attachments = get_attachments(self.doctype, self.name)
		if not attachments:
			frappe.throw(_("Compute the adjustment first: no prepared baselines are attached"))
		content = frappe.get_doc("File", attachments[0].name).get_content()
		return [frappe._dict(row) for row in json.loads(gzip.decompress(content).decode("utf-8"))]

	def post_gl_entries(self, rows: list[frappe._dict]) -> None:
		from erpnext.accounts.general_ledger import make_gl_entries
		from erpnext.stock import get_warehouse_account_map
		from erpnext.stock.services.stock_fold_refold import adjustment_pair

		account_map = get_warehouse_account_map(self.company)
		deltas: dict[str, float] = {}
		for row in rows:
			account = (account_map.get(row.warehouse) or {}).get("account")
			if account and flt(row.delta):
				deltas[account] = deltas.get(account, 0.0) + flt(row.delta)

		args = {
			"voucher_type": self.doctype,
			"voucher_no": self.name,
			"company": self.company,
			"adjustment_remark": _("Stock opening adjustment: engine valuation against legacy balance"),
		}
		gl_map = []
		for account, delta in sorted(deltas.items()):
			if abs(delta) < 0.005:
				continue
			gl_map.extend(adjustment_pair(args, account, self.adjustment_account, delta, self.posting_date))
		if gl_map:
			make_gl_entries(gl_map)

	def shift_bins(self, rows: list[frappe._dict], direction: int) -> None:
		"""Move each differing Bin by the engine-minus-legacy delta; the next
		fold re-projects it exactly from the baseline."""
		from erpnext.stock.services import bin_writer
		from erpnext.stock.utils import get_or_make_bin

		for row in rows:
			qty_shift = flt(row.engine_qty) - flt(row.legacy_qty)
			value_shift = flt(row.delta)
			if abs(qty_shift) <= QTY_TOLERANCE and not value_shift:
				continue
			current = frappe.db.get_value(
				"Bin",
				{"item_code": row.item_code, "warehouse": row.warehouse},
				["name", "actual_qty", "stock_value"],
				as_dict=True,
			) or frappe._dict(name=get_or_make_bin(row.item_code, row.warehouse))
			actual_qty = flt(current.actual_qty) + direction * qty_shift
			stock_value = flt(current.stock_value) + direction * value_shift
			bin_writer.set_fields(
				current.name,
				{
					"actual_qty": actual_qty,
					"stock_value": stock_value,
					"valuation_rate": stock_value / actual_qty if actual_qty else 0.0,
				},
			)


def compute_opening_adjustment(name: str) -> None:
	doc = frappe.get_doc("Stock Opening Adjustment", name)
	try:
		doc.build()
	except Exception:
		frappe.db.rollback()
		doc.db_set("status", "Failed")
		doc.log_error(title="Stock Opening Adjustment Failed")


def _differs(row: frappe._dict) -> bool:
	return bool(flt(row.delta)) or abs(flt(row.engine_qty) - flt(row.legacy_qty)) > QTY_TOLERANCE


def _item_row(row: frappe._dict) -> dict:
	return {
		"item_code": row.item_code,
		"warehouse": row.warehouse,
		"legacy_qty": row.legacy_qty,
		"engine_qty": row.engine_qty,
		"legacy_value": row.legacy_value,
		"engine_value": row.engine_value,
		"delta": row.delta,
	}


def _baseline(row: frappe._dict) -> frappe._dict:
	return frappe._dict(
		item_code=row.item_code,
		warehouse=row.warehouse,
		qty=row.engine_qty,
		rate=row.rate,
		seeds=[frappe._dict(seed) for seed in row.seeds or []],
	)
