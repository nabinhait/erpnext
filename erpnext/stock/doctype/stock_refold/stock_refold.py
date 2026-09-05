# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Stock Refold — the durable queue behind deep backdates and restatements.

A row asks for one key to be refolded from an instant. Deep backdates (past
``REFOLD_CAP``) queue one after valuing their own row synchronously; a
Stock Restatement queues one per key of the reopened period. Rows are
processed oldest first by a long-queue job kicked on enqueue and, as the
safety net, by the hourly scheduler; the job re-kicks itself while rows
remain and stops within its time budget.
"""

import time
import traceback

import frappe
from frappe.model.document import Document
from frappe.utils import nowdate
from frappe.utils.background_jobs import enqueue

JOB_ID = "stock_refold_queue"
TIME_BUDGET = 25 * 60


class StockRefold(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link | None
		error: DF.SmallText | None
		from_datetime: DF.Datetime
		item_code: DF.Link
		status: DF.Literal["Queued", "In Progress", "Completed", "Failed"]
		stock_restatement: DF.Link | None
		voucher_no: DF.DynamicLink | None
		voucher_type: DF.Link | None
		warehouse: DF.Link
	# end: auto-generated types

	pass


def enqueue_refold(
	item_code: str,
	warehouse: str,
	company: str | None,
	from_datetime,
	voucher_type: str | None = None,
	voucher_no: str | None = None,
	restatement: str | None = None,
) -> str:
	"""One queued row per key: an earlier instant widens the pending row
	instead of adding another. Restatement rows are processed by the
	restatement job, everything else kicks the queue worker."""
	pending = frappe.db.get_value(
		"Stock Refold",
		{"item_code": item_code, "warehouse": warehouse, "status": "Queued"},
		["name", "from_datetime"],
		as_dict=True,
	)
	if pending:
		if str(from_datetime) < str(pending.from_datetime):
			frappe.db.set_value("Stock Refold", pending.name, "from_datetime", str(from_datetime))
		return pending.name

	row = frappe.get_doc(
		doctype="Stock Refold",
		item_code=item_code,
		warehouse=warehouse,
		company=company,
		from_datetime=str(from_datetime),
		voucher_type=voucher_type,
		voucher_no=voucher_no,
		stock_restatement=restatement,
	).insert(ignore_permissions=True)
	if not restatement:
		kick()
	return row.name


def kick() -> None:
	enqueue(process_refold_queue, queue="long", timeout=3600, job_id=JOB_ID, deduplicate=True)


def process_refold_queue(restatement: str | None = None, time_budget: int = TIME_BUDGET) -> dict:
	"""Refold queued rows oldest first until none remain or the budget is
	spent; finalizes any restatement whose rows are all done."""
	from erpnext.stock.doctype.stock_restatement.stock_restatement import finalize_if_done

	started = time.monotonic()
	report = {"completed": 0, "failed": 0}
	restatements = set()
	while time.monotonic() - started < time_budget:
		row = _next_queued(restatement)
		if not row:
			break
		_run(row, report)
		if row.stock_restatement:
			restatements.add(row.stock_restatement)

	for name in restatements:
		finalize_if_done(name)
	if _next_queued(restatement) and not frappe.in_test:
		kick()
	return report


def _next_queued(restatement: str | None) -> frappe._dict | None:
	filters = {"status": "Queued"}
	if restatement:
		filters["stock_restatement"] = restatement
	rows = frappe.get_all(
		"Stock Refold",
		filters=filters,
		fields=[
			"name",
			"item_code",
			"warehouse",
			"company",
			"from_datetime",
			"voucher_type",
			"voucher_no",
			"stock_restatement",
		],
		order_by="creation",
		limit=1,
	)
	return rows[0] if rows else None


def _run(row: frappe._dict, report: dict) -> None:
	from erpnext.stock.services.stock_fold_refold import refold_key

	frappe.db.set_value("Stock Refold", row.name, "status", "In Progress")
	_commit()
	try:
		done = refold_key(row.item_code, row.warehouse, str(row.from_datetime), _args(row))
		error = None if done else "key cannot fold: legacy values kept"
	except Exception:
		if frappe.in_test:
			raise
		frappe.db.rollback()
		done, error = False, traceback.format_exc()
		frappe.log_error(title="Stock Refold failed", message=error)
	frappe.db.set_value(
		"Stock Refold", row.name, {"status": "Completed" if done else "Failed", "error": error}
	)
	report["completed" if done else "failed"] += 1
	_commit()


def _args(row: frappe._dict) -> dict:
	"""GL corrections ride on the voucher that caused the refold: the
	backdated voucher, or the restatement itself (which always books
	append-only corrections)."""
	if row.stock_restatement:
		return {
			"company": row.company,
			"voucher_type": "Stock Restatement",
			"voucher_no": row.stock_restatement,
			"posting_date": nowdate(),
			"adjustment_remark": "Stock value restatement after reopening the period",
			"force_gl_adjustment": True,
		}
	return {
		"company": row.company,
		"voucher_type": row.voucher_type,
		"voucher_no": row.voucher_no,
		"posting_date": nowdate(),
	}


def _commit() -> None:
	if not frappe.in_test:
		frappe.db.commit()
