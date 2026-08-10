# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Detect writes to `tabStock Ledger Entry` / `tabBin` that bypass the writer modules.

The writer modules mark their writes with a request-local token; document-API
writes on the two doctypes check for it and log an Error Log entry with the
offending traceback when it is missing. Off by default — enable with
``log_unrouted_stock_writes: 1`` in site config. This catches document-API
writers (including third-party apps using ``get_doc``/``db_set``); raw SQL
writers can only be caught by database triggers, which this layer does not
attempt.
"""

import traceback
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps

import frappe

_FLAG = "stock_write_authorized"


def authorized_writer(func: Callable) -> Callable:
	"""Mark every write inside the decorated writer primitive as routed."""

	@wraps(func)
	def wrapper(*args, **kwargs):
		with _authorized():
			return func(*args, **kwargs)

	return wrapper


def check(table: str) -> None:
	"""Log a write reaching ``table`` without passing through its writer module."""
	if getattr(frappe.local, _FLAG, 0):
		return

	if not frappe.conf.get("log_unrouted_stock_writes"):
		return

	frappe.log_error(
		title=f"Unrouted write to {table}",
		message="".join(traceback.format_stack(limit=25)),
	)


@contextmanager
def _authorized():
	previous = getattr(frappe.local, _FLAG, 0)
	setattr(frappe.local, _FLAG, previous + 1)
	try:
		yield
	finally:
		setattr(frappe.local, _FLAG, previous)
