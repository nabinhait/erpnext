# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document


class StockOpeningAdjustmentItem(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		delta: DF.Currency
		engine_qty: DF.Float
		engine_value: DF.Currency
		item_code: DF.Link | None
		legacy_qty: DF.Float
		legacy_value: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		warehouse: DF.Link | None
	# end: auto-generated types

	pass
