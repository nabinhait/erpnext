# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""The §2.11 perf gate, measured on a real site.

Times submits at increasing history depth under fold authority (cold =
checkpoint rebuild, warm = fold-forward) and a mid-history backdate (the
synchronous refold), against the legacy engine including its Repost Item
Valuation processing time. Runs in one transaction and rolls back — the
site is left untouched.

    bench --site <site> execute erpnext.stock.services.stock_fold_perf.run
"""

import time

import frappe
from frappe.utils import flt

FLAGS = ("stock_event_dual_write", "stock_fold_authoritative", "stock_fold_suppress_legacy_repost")


def run() -> dict:
	keys = _depth_ladder()
	if len(keys) < 4:
		frappe.throw("Not enough plain-item keys for a depth ladder")

	out = {"ladder": [{"depth": key.depth, "key": f"{key.item_code} @ {key.warehouse}"} for key in keys]}

	# measure engine cost, not site automation: doc-event scripts on stock
	# entries are disabled inside this rolled-back transaction only
	for name in frappe.get_all(
		"Server Script",
		filters={"disabled": 0, "script_type": "DocType Event", "reference_doctype": "Stock Entry"},
		pluck="name",
	):
		frappe.db.set_value("Server Script", name, "disabled", 1)
	frappe.client_cache.delete_value("server_script_map")

	for flag in FLAGS:
		frappe.conf[flag] = 1
	try:
		_warmup(keys[-1])
		out["fold"] = _measure(keys[:2], backdate_key=keys[0])
	finally:
		for flag in FLAGS:
			frappe.conf.pop(flag, None)

	out["legacy"] = _measure(keys[2:4], backdate_key=keys[2], process_reposts=True)

	frappe.db.rollback()
	return out


def _measure(keys: list, backdate_key, process_reposts: bool = False) -> dict:
	result = {}
	for label, key in zip(("deep", "mid"), keys, strict=False):
		cold, _ = _timed_submit(key)
		warm, _ = _timed_submit(key)
		result[f"{label}_submit_cold_ms"] = round(cold, 1)
		result[f"{label}_submit_warm_ms"] = round(warm, 1)

	backdate_ms, voucher = _timed_submit(backdate_key, posting_date=_mid_history_date(backdate_key))
	result["backdate_submit_ms"] = round(backdate_ms, 1)

	if process_reposts:
		from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

		pending = frappe.get_all(
			"Repost Item Valuation",
			filters={"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
			pluck="name",
		)
		start = time.perf_counter()
		for name in pending:
			repost(frappe.get_doc("Repost Item Valuation", name))
		result["repost_processing_ms"] = round((time.perf_counter() - start) * 1000, 1)
		result["reposts_processed"] = len(pending)

	return result


def _timed_submit(key, posting_date=None) -> tuple[float, str]:
	from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

	start = time.perf_counter()
	entry = make_stock_entry(
		item_code=key.item_code,
		target=key.warehouse,
		qty=1,
		rate=100,
		company=key.company,
		posting_date=posting_date,
		batch_no=key.batch_no,
	)
	return (time.perf_counter() - start) * 1000, entry.name


def _depth_ladder() -> list[frappe._dict]:
	"""Deepest keys: two for the fold run, two comparable for legacy. This site
	is batch-tracked throughout, so submits reference an existing batch via the
	legacy fields."""
	closed_until = frappe.db.get_value(
		"Period Closing Voucher", {"docstatus": 1}, "period_end_date", order_by="period_end_date desc"
	)
	keys = frappe.db.sql(
		"""
		SELECT e.item_code, e.warehouse, COUNT(*) AS depth, MAX(w.company) AS company
		FROM `tabStock Event` e
		JOIN `tabItem` i ON i.name = e.item_code
		JOIN `tabWarehouse` w ON w.name = e.warehouse
		WHERE IFNULL(i.disabled, 0) = 0 AND i.is_stock_item = 1
			AND IFNULL(i.has_serial_no, 0) = 0
		GROUP BY e.item_code, e.warehouse
		HAVING SUM(e.posting_datetime > %s) >= 3
		ORDER BY depth DESC
		LIMIT 4
		""",
		str(closed_until or "1900-01-01") + " 23:59:59",
		as_dict=True,
	)
	for key in keys:
		key.batch_no = frappe.db.get_value("Batch", {"item": key.item_code}, "name")
	return keys


def _mid_history_date(key) -> str:
	"""Midpoint of the key's still-open history — closed books reject backdates."""
	filters = {"item_code": key.item_code, "warehouse": key.warehouse}
	closed_until = frappe.db.get_value(
		"Period Closing Voucher", {"docstatus": 1}, "period_end_date", order_by="period_end_date desc"
	)
	if closed_until:
		filters["posting_datetime"] = (">", str(closed_until) + " 23:59:59")

	dates = frappe.get_all(
		"Stock Event", filters=filters, pluck="posting_datetime", order_by="posting_datetime"
	)
	if not dates:
		frappe.throw(f"No open-period history for {key.item_code} @ {key.warehouse}")
	return str(dates[len(dates) // 2].date())


def _warmup(key) -> None:
	"""One throwaway submit so meta/caches don't bill the first measurement."""
	_timed_submit(key)
