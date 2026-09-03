# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Ledger computed from facts — running balances on read.

Each row is one event with its running quantity and value from the fold
(nearest checkpoint + tail); stored SLE values are never consulted."""

import frappe
from frappe import _

from erpnext.stock.services import stock_fold_read


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_data(filters) -> list[dict]:
	from_boundary = str(filters.from_date) + " 00:00:00" if filters.get("from_date") else "1900-01-01"
	to_boundary = str(filters.to_date) + " 23:59:59.999999" if filters.get("to_date") else "9999-12-31"

	rows = []
	for key in _keys(filters):
		for row in stock_fold_read.ledger_rows(key.item_code, key.warehouse, from_boundary, to_boundary):
			qty = row["qty_change"]
			rows.append(
				{
					"date": row["posting_datetime"],
					"item_code": key.item_code,
					"warehouse": key.warehouse,
					"in_qty": qty if qty > 0 else 0,
					"out_qty": -qty if qty < 0 else 0,
					"qty_after_transaction": row["qty_after"],
					"valuation_rate": row["valuation_rate"],
					"stock_value": row["value_after"],
					"stock_value_difference": row["value_delta"],
					"voucher_type": row["voucher_type"],
					"voucher_no": row["voucher_no"],
					"sle": row["sle"],
				}
			)

	rows.sort(key=lambda row: (str(row["date"]), row["item_code"], row["warehouse"]))
	return rows


def _keys(filters) -> list[frappe._dict]:
	event = frappe.qb.DocType("Stock Event")
	query = frappe.qb.from_(event).select(event.item_code, event.warehouse).distinct()
	if filters.get("company"):
		query = query.where(event.company == filters.company)
	if filters.get("item_code"):
		query = query.where(event.item_code == filters.item_code)
	if filters.get("warehouse"):
		query = query.where(event.warehouse == filters.warehouse)
	return query.orderby(event.item_code).orderby(event.warehouse).run(as_dict=True)


def get_columns() -> list[dict]:
	return [
		{"label": _("Date"), "fieldname": "date", "fieldtype": "Datetime", "width": 150},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 150},
		{"label": _("Warehouse"), "fieldname": "warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 150},
		{"label": _("In Qty"), "fieldname": "in_qty", "fieldtype": "Float", "width": 80},
		{"label": _("Out Qty"), "fieldname": "out_qty", "fieldtype": "Float", "width": 80},
		{"label": _("Balance Qty"), "fieldname": "qty_after_transaction", "fieldtype": "Float", "width": 100},
		{"label": _("Valuation Rate"), "fieldname": "valuation_rate", "fieldtype": "Float", "width": 100},
		{"label": _("Balance Value"), "fieldname": "stock_value", "fieldtype": "Float", "width": 110},
		{"label": _("Value Change"), "fieldname": "stock_value_difference", "fieldtype": "Float", "width": 110},
		{"label": _("Voucher Type"), "fieldname": "voucher_type", "fieldtype": "Data", "width": 120},
		{"label": _("Voucher No"), "fieldname": "voucher_no", "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 140},
	]
