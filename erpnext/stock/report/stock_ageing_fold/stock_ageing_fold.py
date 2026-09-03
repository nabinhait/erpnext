# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Ageing computed from facts — layers straight from the fold.

The fold's surviving layers carry their source event, so age is simply
to_date minus the source's posting date: no stock_queue JSON, no drift.
Lot-tracked keys age per lot the same way."""

import frappe
from frappe import _
from frappe.utils import date_diff, flt

from erpnext.stock.services import stock_fold_read

DEFAULT_RANGES = (30, 60, 90)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(filters), get_data(filters)


def get_data(filters) -> list[dict]:
	as_of = str(filters.get("to_date") or frappe.utils.nowdate())
	boundary = as_of + " 23:59:59.999999"

	rows = []
	for key in _keys(filters):
		state = stock_fold_read.state_as_of(key.item_code, key.warehouse, boundary)
		layers = list(state.layers)
		for lot in state.lots:
			layers.extend(lot.state.layers)
		if not layers:
			continue

		ages = _layer_ages(layers, as_of)
		total_qty = sum(layer.qty for layer in layers)
		if not total_qty:
			continue

		row = {
			"item_code": key.item_code,
			"warehouse": key.warehouse,
			"qty": total_qty,
			"average_age": sum(age * layer.qty for layer, age in ages) / total_qty,
			"earliest": max(age for _, age in ages),
			"latest": min(age for _, age in ages),
		}
		for index, upper in enumerate((*DEFAULT_RANGES, None), start=1):
			lower = ([0, *DEFAULT_RANGES])[index - 1]
			in_range = [
				(layer, age)
				for layer, age in ages
				if age >= lower and (upper is None or age < upper)
			]
			row[f"range{index}"] = sum(layer.qty for layer, _ in in_range)
			row[f"range{index}value"] = sum(layer.qty * layer.rate for layer, _ in in_range)
		rows.append(row)

	return rows


def _layer_ages(layers, as_of: str) -> list[tuple]:
	sources = {layer.source_event_id for layer in layers if layer.source_event_id}
	dates = {}
	if sources:
		dates = {
			int(row.name): str(row.posting_datetime)[:10]
			for row in frappe.get_all(
				"Stock Event", filters={"name": ("in", list(sources))}, fields=["name", "posting_datetime"]
			)
		}
	return [
		(layer, date_diff(as_of, dates.get(layer.source_event_id, as_of)) if layer.source_event_id else 0)
		for layer in layers
	]


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


def get_columns(filters) -> list[dict]:
	columns = [
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 150},
		{"label": _("Warehouse"), "fieldname": "warehouse", "fieldtype": "Link", "options": "Warehouse", "width": 150},
		{"label": _("Available Qty"), "fieldname": "qty", "fieldtype": "Float", "width": 100},
		{"label": _("Average Age"), "fieldname": "average_age", "fieldtype": "Float", "width": 100},
		{"label": _("Earliest (days)"), "fieldname": "earliest", "fieldtype": "Int", "width": 90},
		{"label": _("Latest (days)"), "fieldname": "latest", "fieldtype": "Int", "width": 90},
	]
	labels = ["0-30", "30-60", "60-90", "90+"]
	for index, label in enumerate(labels, start=1):
		columns.append({"label": _(f"Age ({label})"), "fieldname": f"range{index}", "fieldtype": "Float", "width": 110})
		columns.append({"label": _(f"Value ({label})"), "fieldname": f"range{index}value", "fieldtype": "Float", "width": 110})
	return columns
