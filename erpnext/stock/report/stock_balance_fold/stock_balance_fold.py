# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Balance computed from facts — the v17 read model.

Opening and closing come from ``stock_fold_read.state_as_of`` (nearest
checkpoint plus folded tail); in/out movement is aggregated from the window's
events. No stored Stock Ledger Entry value is consulted anywhere, and
closings bound every read, so runtime does not grow with history depth."""

import frappe
from frappe import _

from erpnext.stock.services import stock_fold_read


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_data(filters) -> list[dict]:
	from_boundary = str(filters.from_date) + " 00:00:00" if filters.get("from_date") else None
	to_boundary = str(filters.to_date) + " 23:59:59.999999" if filters.get("to_date") else "9999-12-31"

	rows = []
	for key in _keys(filters):
		opening = None
		if from_boundary:
			opening = stock_fold_read.state_as_of(key.item_code, key.warehouse, from_boundary)

		window = stock_fold_read.ledger_rows(
			key.item_code, key.warehouse, from_boundary or "1900-01-01", to_boundary
		)

		opening_qty = opening.qty if opening else 0.0
		opening_val = _value(opening) if opening else 0.0
		in_qty = sum(row["qty_change"] for row in window if row["qty_change"] > 0)
		out_qty = -sum(row["qty_change"] for row in window if row["qty_change"] < 0)
		in_val = sum(row["value_delta"] for row in window if row["value_delta"] > 0)
		out_val = -sum(row["value_delta"] for row in window if row["value_delta"] < 0)

		if window:
			bal_qty = window[-1]["qty_after"]
			bal_val = window[-1]["value_after"]
			val_rate = window[-1]["valuation_rate"]
		else:
			bal_qty, bal_val = opening_qty, opening_val
			val_rate = (bal_val / bal_qty) if bal_qty else 0.0

		if not (bal_qty or bal_val or in_qty or out_qty or opening_qty or opening_val):
			continue

		rows.append(
			{
				"item_code": key.item_code,
				"warehouse": key.warehouse,
				"opening_qty": opening_qty,
				"opening_val": opening_val,
				"in_qty": in_qty,
				"in_val": in_val,
				"out_qty": out_qty,
				"out_val": out_val,
				"bal_qty": bal_qty,
				"bal_val": bal_val,
				"val_rate": val_rate,
			}
		)

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


def _value(state) -> float:
	from erpnext.stock.services import stock_engine_bridge

	return stock_engine_bridge.equivalent_value(state)


def get_columns() -> list[dict]:
	return [
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 160},
		{
			"label": _("Warehouse"),
			"fieldname": "warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 160,
		},
		{"label": _("Opening Qty"), "fieldname": "opening_qty", "fieldtype": "Float", "width": 100},
		{"label": _("Opening Value"), "fieldname": "opening_val", "fieldtype": "Float", "width": 110},
		{"label": _("In Qty"), "fieldname": "in_qty", "fieldtype": "Float", "width": 90},
		{"label": _("In Value"), "fieldname": "in_val", "fieldtype": "Float", "width": 100},
		{"label": _("Out Qty"), "fieldname": "out_qty", "fieldtype": "Float", "width": 90},
		{"label": _("Out Value"), "fieldname": "out_val", "fieldtype": "Float", "width": 100},
		{"label": _("Balance Qty"), "fieldname": "bal_qty", "fieldtype": "Float", "width": 100},
		{"label": _("Balance Value"), "fieldname": "bal_val", "fieldtype": "Float", "width": 110},
		{"label": _("Valuation Rate"), "fieldname": "val_rate", "fieldtype": "Float", "width": 100},
	]
