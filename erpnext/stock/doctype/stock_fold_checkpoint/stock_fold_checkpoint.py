# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Fold Checkpoint — fold-resumable state for one key at a closing.

Disposable derived state: any checkpoint can be rebuilt by folding the key's
events up to its ``as_of``. Written by ``stock_fold_read.create_checkpoints``
when a Stock Closing Entry is processed; every fold-based read starts from
the nearest checkpoint instead of the beginning of history."""

import frappe
from frappe.model.document import Document


class StockFoldCheckpoint(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		as_of: DF.Datetime
		item_code: DF.Link
		last_event: DF.Int
		state_json: DF.LongText | None
		stock_closing_entry: DF.Link | None
		warehouse: DF.Link
	# end: auto-generated types

	pass


def on_doctype_update():
	frappe.db.add_index("Stock Fold Checkpoint", ["item_code", "warehouse", "as_of"])
