# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Restatement — reopening a frontier year (design doc Part 4).

A fold is path-dependent, so cancelling the frontier's Stock Closing Entry
restates the whole reopened period to engine truth instead of repricing
only what a backdate touched. The job slides the frontier one closing back
(a submitted closing at the previous frontier and its Stock Opening
Adjustment, auto-submitted within the threshold or when zero), queues one
Stock Refold per key with events after that frontier, and works through
them; GL corrections are append-only rows carried on the restatement,
dated at each affected voucher's own posting date. Stock stays locked up
to the reopened date while the restatement runs. Resumable: the hourly
queue worker finishes the rows and finalizes the document.
"""

import traceback
from collections import Counter

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, flt
from frappe.utils.background_jobs import enqueue

from erpnext.stock.services import stock_engine_bridge

RUNNING = ("Queued", "In Progress")


class StockRestatement(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cancelled_closing_entry: DF.Link | None
		company: DF.Link
		error: DF.SmallText | None
		from_date: DF.Date | None
		frontier_closing_entry: DF.Link | None
		keys_done: DF.Int
		keys_failed: DF.Int
		keys_total: DF.Int
		naming_series: DF.Literal["SRST-.YYYY.-.#####"]
		opening_adjustment: DF.Link | None
		status: DF.Literal["Queued", "In Progress", "Completed", "Failed"]
		to_date: DF.Date | None
	# end: auto-generated types

	@property
	def moment(self) -> str:
		return stock_engine_bridge.end_of_day(self.from_date)


def running_restatement(company: str) -> frappe._dict | None:
	"""The restatement currently locking the company's stock, if any."""
	return frappe.db.get_value(
		"Stock Restatement",
		{"company": company, "status": ("in", RUNNING)},
		["name", "to_date"],
		as_dict=True,
		order_by="to_date desc",
	)


def start_for_closing(closing) -> str:
	"""Queue the restatement for a cancelled frontier closing."""
	doc = frappe.get_doc(
		doctype="Stock Restatement",
		company=closing.company,
		cancelled_closing_entry=closing.name,
		from_date=_previous_frontier(closing),
		to_date=closing.to_date,
	).insert(ignore_permissions=True)
	enqueue(run_restatement, name=doc.name, queue="long", timeout=4 * 3600)
	return doc.name


def run_restatement(name: str) -> None:
	from erpnext.stock.doctype.stock_refold.stock_refold import process_refold_queue

	doc = frappe.get_doc("Stock Restatement", name)
	try:
		doc.db_set("status", "In Progress")
		_slide_frontier(doc)
		_queue_keys(doc)
		if not frappe.in_test:
			frappe.db.commit()
		process_refold_queue(restatement=doc.name)
	except Exception:
		if frappe.in_test:
			raise
		frappe.db.rollback()
		doc.db_set({"status": "Failed", "error": traceback.format_exc()})
		doc.log_error(title="Stock Restatement failed")


def finalize_if_done(name: str) -> None:
	"""Completed once every queued key has run; Failed if any key could
	not fold (its legacy values stay and the row carries the reason)."""
	counts = Counter(frappe.get_all("Stock Refold", filters={"stock_restatement": name}, pluck="status"))
	if counts.get("Queued") or counts.get("In Progress"):
		return
	failed = counts.get("Failed", 0)
	frappe.db.set_value(
		"Stock Restatement",
		name,
		{
			"status": "Failed" if failed else "Completed",
			"keys_done": counts.get("Completed", 0),
			"keys_failed": failed,
		},
	)


def _previous_frontier(closing) -> str:
	"""The closing before the reopened one, or the day before its fiscal year."""
	from erpnext.accounts.utils import get_fiscal_year

	previous = frappe.db.get_value(
		"Stock Closing Entry",
		{"company": closing.company, "docstatus": 1, "to_date": ("<", closing.to_date)},
		"to_date",
		order_by="to_date desc",
	)
	if previous:
		return str(previous)
	fiscal_year = get_fiscal_year(closing.to_date, company=closing.company, as_dict=True)
	return str(add_days(fiscal_year.year_start_date, -1))


def _slide_frontier(doc: StockRestatement) -> None:
	"""A submitted closing at the previous frontier (created if missing) and
	the opening adjustment that pins engine truth there."""
	closing_name = frappe.db.get_value(
		"Stock Closing Entry", {"company": doc.company, "docstatus": 1, "to_date": doc.from_date}, "name"
	)
	if not closing_name:
		closing = frappe.get_doc(
			doctype="Stock Closing Entry", company=doc.company, from_date=doc.from_date, to_date=doc.from_date
		)
		closing.submit()
		closing_name = closing.name
	doc.db_set({"frontier_closing_entry": closing_name, "opening_adjustment": _adjust(doc, closing_name)})


def _adjust(doc: StockRestatement, closing_name: str) -> str:
	live = frappe.db.get_value(
		"Stock Opening Adjustment", {"stock_closing_entry": closing_name, "docstatus": 1}, "name"
	)
	if live:
		return live
	adjustment = frappe.get_doc(
		doctype="Stock Opening Adjustment", company=doc.company, stock_closing_entry=closing_name
	).insert(ignore_permissions=True)
	adjustment.build()
	if adjustment.within_threshold or not flt(adjustment.total_delta):
		adjustment.submit()
	return adjustment.name


def _queue_keys(doc: StockRestatement) -> None:
	from erpnext.stock.doctype.stock_refold.stock_refold import enqueue_refolds
	from erpnext.stock.services.stock_fold_read import active_keys

	keys = active_keys(doc.company, after=doc.moment)
	enqueue_refolds(keys, doc.company, doc.moment, doc.name)
	doc.db_set("keys_total", len(keys))
