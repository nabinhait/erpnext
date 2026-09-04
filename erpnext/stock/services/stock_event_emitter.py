# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Single write path for `tabStock Event`.

Maps a Stock Ledger Entry to its fact representation and inserts it. Used by
the dual-write hook in ``stock_ledger_writer.submit_new`` (site config
``stock_event_dual_write``) and by the historical backfill — both go through
:func:`event_args_from_sle`, so a backfilled event and a dual-written event of
the same SLE are byte-identical, hash included.
"""

import hashlib
from typing import TYPE_CHECKING

import frappe
from frappe.utils import flt

if TYPE_CHECKING:
	from frappe.model.document import Document

HASH_FIELDS = (
	"item_code",
	"warehouse",
	"posting_datetime",
	"kind",
	"qty_change",
	"declared_rate",
	"assert_qty",
	"assert_rate",
	"voucher_type",
	"voucher_no",
	"voucher_detail_no",
	"sle",
)


def emit_for_sle(sle: "Document | dict", source: str = "Dual Write") -> frappe._dict:
	"""Insert the Stock Event fact for one Stock Ledger Entry.

	Hot path: a sequence-reserved direct insert (no doc machinery), with the
	fresh event stashed on frappe.local so fold authority in the same request
	folds it without re-reading it."""
	args = event_args_from_sle(sle)
	args["source"] = source

	if args["kind"] == "Reversal":
		args["reverses_event"] = _find_reversed_event(args)

	event_id = frappe.db.get_next_sequence_val("Stock Event")
	timestamp = frappe.utils.now()
	row = {
		**{key: value for key, value in args.items() if key not in ("doctype", "allocations")},
		"name": event_id,
		"creation": timestamp,
		"modified": timestamp,
		"owner": "Administrator",
		"modified_by": "Administrator",
	}
	frappe.db.bulk_insert("Stock Event", tuple(row), [list(row.values())])

	allocation_rows = [
		[
			frappe.generate_hash(length=10),
			str(event_id),
			"Stock Event",
			"allocations",
			position,
			allocation.get("serial_no"),
			allocation.get("batch_no"),
			allocation.get("qty_change"),
			allocation.get("declared_rate"),
			timestamp,
			timestamp,
			"Administrator",
			"Administrator",
		]
		for position, allocation in enumerate(args.get("allocations") or [], start=1)
	]
	if allocation_rows:
		frappe.db.bulk_insert("Stock Event Allocation", ALLOCATION_FIELDS, allocation_rows)

	event = frappe._dict(args, name=event_id)
	frappe.local.stock_event_last_emitted = event
	return event


ALLOCATION_FIELDS = (
	"name",
	"parent",
	"parenttype",
	"parentfield",
	"idx",
	"serial_no",
	"batch_no",
	"qty_change",
	"declared_rate",
	"creation",
	"modified",
	"owner",
	"modified_by",
)


def emit_revaluation(
	item_code: str,
	warehouse: str,
	company: str,
	posting_datetime: str,
	source_event: int,
	value_change: float,
	voucher_type: str,
	voucher_no: str,
) -> frappe._dict:
	"""Insert a Revaluation fact: a cost revision on a prior receipt, ordered
	at the receipt's own instant so the refold trues up everything after it."""
	event_id = frappe.db.get_next_sequence_val("Stock Event")
	timestamp = frappe.utils.now()
	row = {
		"name": event_id,
		"item_code": item_code,
		"warehouse": warehouse,
		"company": company,
		"posting_datetime": str(posting_datetime),
		"kind": "Revaluation",
		"qty_change": 0.0,
		"declared_rate": 0.0,
		"assert_qty": 0.0,
		"assert_rate": 0.0,
		"reverses_event": source_event,
		"value_change": flt(value_change),
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"source": "Dual Write",
		"creation": timestamp,
		"modified": timestamp,
		"owner": "Administrator",
		"modified_by": "Administrator",
	}
	frappe.db.bulk_insert("Stock Event", tuple(row), [list(row.values())])
	return frappe._dict(row)


def event_args_from_sle(sle: "Document | dict", allocations: list[dict] | None = None) -> dict:
	"""Map an SLE to fact fields — declared data only, no derived valuation.

	Pass ``allocations`` to skip the per-bundle lookup (bulk backfill
	pre-fetches them); the content hash does not cover allocations, so the
	result is identical either way."""
	kind = _kind(sle)
	args = {
		"doctype": "Stock Event",
		"item_code": sle.get("item_code"),
		"warehouse": sle.get("warehouse"),
		"company": sle.get("company"),
		"posting_datetime": str(sle.get("posting_datetime")),
		"kind": kind,
		"qty_change": flt(sle.get("actual_qty")),
		"declared_rate": _declared_rate(sle),
		"assert_qty": flt(sle.get("qty_after_transaction")) if kind == "Assertion" else 0.0,
		"assert_rate": flt(sle.get("valuation_rate")) if kind == "Assertion" else 0.0,
		"voucher_type": sle.get("voucher_type"),
		"voucher_no": sle.get("voucher_no"),
		"voucher_detail_no": sle.get("voucher_detail_no"),
		"sle": sle.get("name"),
		"allocations": _allocations(sle) if allocations is None else allocations,
	}
	args["content_hash"] = content_hash(args)
	return args


def content_hash(args: dict) -> str:
	canonical = "|".join(repr(args.get(field)) for field in HASH_FIELDS)
	return hashlib.sha256(canonical.encode()).hexdigest()


def delete_for_voucher(voucher_type: str, voucher_no: str) -> None:
	"""Remove a voucher's events when its ledger rows are hard-deleted."""
	frappe.db.delete("Stock Event", {"voucher_type": voucher_type, "voucher_no": voucher_no})


def _declared_rate(sle: "Document | dict") -> float:
	"""What the business declared: incoming rate on receipts; on issues, only
	cost-linked legs carry a rate — inward voucher types issuing stock are
	moving specific stock (transit consumption, purchase returns) at the
	linked rate legacy stored as outgoing_rate."""
	if flt(sle.get("actual_qty")) > 0:
		return flt(sle.get("incoming_rate"))
	if sle.get("voucher_type") in ("Purchase Receipt", "Purchase Invoice"):
		return flt(sle.get("outgoing_rate"))
	return 0.0


def _kind(sle: "Document | dict") -> str:
	if sle.get("is_cancelled"):
		return "Reversal"
	if sle.get("voucher_type") == "Stock Reconciliation":
		return "Assertion"
	return "Receipt" if flt(sle.get("actual_qty")) > 0 else "Issue"


def _allocations(sle: "Document | dict") -> list[dict]:
	bundle = sle.get("serial_and_batch_bundle")
	if not bundle:
		return _field_allocations(sle)

	entries = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle},
		fields=["serial_no", "batch_no", "qty", "incoming_rate"],
		order_by="idx",
	)
	rows = [
		{
			"serial_no": row.serial_no,
			"batch_no": row.batch_no,
			"qty_change": flt(row.qty),
			"declared_rate": flt(row.incoming_rate),
		}
		for row in entries
	]
	# a cancellation's SLE reuses the original bundle, so entry signs carry the
	# bundle's direction; the SLE dictates the movement's — flip when opposed
	total = sum(row["qty_change"] for row in rows)
	if total * flt(sle.get("actual_qty")) < 0:
		for row in rows:
			row["qty_change"] = -row["qty_change"]
	return rows


def _field_allocations(sle: "Document | dict") -> list[dict]:
	"""Lot facts for pre-bundle rows: v14-era SLEs carry batch_no/serial_no fields."""
	qty = flt(sle.get("actual_qty"))
	serials = (sle.get("serial_no") or "").strip()
	if serials:
		names = [name.strip() for name in serials.split("\n") if name.strip()]
		if names and abs(qty) == len(names):
			sign = 1 if qty > 0 else -1
			return [{"serial_no": name, "batch_no": None, "qty_change": sign} for name in names]
		return []

	batch = sle.get("batch_no")
	if batch and qty:
		return [{"serial_no": None, "batch_no": batch, "qty_change": qty}]
	return []


def _find_reversed_event(args: dict) -> int | None:
	filters = {
		"voucher_type": args["voucher_type"],
		"voucher_no": args["voucher_no"],
		"warehouse": args["warehouse"],
		"kind": ("!=", "Reversal"),
		"qty_change": -flt(args["qty_change"]),
	}
	if args.get("voucher_detail_no"):
		filters["voucher_detail_no"] = args["voucher_detail_no"]

	return frappe.db.get_value("Stock Event", filters, "name")
