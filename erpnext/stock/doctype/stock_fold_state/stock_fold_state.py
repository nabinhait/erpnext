# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Fold State — the persisted fold checkpoint for one stock key.

Disposable derived state (tier 2): deleting a row costs one replay of the
key's events. Written only by ``stock_fold_authority``; invalidated whenever
the legacy engine rewrites the key's valuation.
"""

import frappe
from frappe.model.document import Document


class StockFoldState(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		item_code: DF.Link
		last_event: DF.Int
		state_json: DF.LongText | None
		warehouse: DF.Link
	# end: auto-generated types

	pass


def on_doctype_update():
	frappe.db.add_unique("Stock Fold State", ["item_code", "warehouse"])
