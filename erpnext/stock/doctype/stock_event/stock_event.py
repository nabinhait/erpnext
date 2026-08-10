# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Event — an immutable stock fact.

Facts record only what the business declared (quantity moved, declared rate,
counted balance); no derived valuation ever lands here. Rows are written
exclusively by ``erpnext.stock.services.stock_event_emitter`` (dual-write and
backfill); nothing edits or renames them afterwards. The order key is
``(posting_datetime, name)`` where ``name`` is the auto-increment id.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class StockEvent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.stock.doctype.stock_event_allocation.stock_event_allocation import (
			StockEventAllocation,
		)

		allocations: DF.Table[StockEventAllocation]
		assert_qty: DF.Float
		assert_rate: DF.Float
		company: DF.Link | None
		content_hash: DF.Data | None
		declared_rate: DF.Float
		item_code: DF.Link
		kind: DF.Literal["Receipt", "Issue", "Assertion", "Reversal"]
		posting_datetime: DF.Datetime
		qty_change: DF.Float
		reverses_event: DF.Int
		sle: DF.Link | None
		source: DF.Literal["Dual Write", "Backfill"]
		voucher_detail_no: DF.Data | None
		voucher_no: DF.DynamicLink | None
		voucher_type: DF.Link | None
		warehouse: DF.Link
	# end: auto-generated types

	def on_update(self):
		if not self.flags.via_stock_event_emitter:
			frappe.throw(_("Stock Events are immutable facts and can only be written by the emitter"))


def on_doctype_update():
	frappe.db.add_index("Stock Event", ["item_code", "warehouse", "posting_datetime"])
