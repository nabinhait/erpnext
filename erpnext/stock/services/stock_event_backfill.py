# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Backfill Stock Events from historical Stock Ledger Entries.

Converts live (non-cancelled) SLEs to facts in the legacy total order
``(posting_datetime, creation, name)`` per warehouse, so the fact order key
``(posting_datetime, id)`` reproduces the legacy ordering exactly. Chunked,
idempotent, and resumable: rerunning skips SLEs that already have an event
(unique ``sle`` link). Run and verify from the bench console:

    bench --site <site> execute erpnext.stock.services.stock_event_backfill.run
    bench --site <site> execute erpnext.stock.services.stock_event_backfill.verify
"""

import frappe
from frappe.utils import cint

from erpnext.stock.services import stock_event_emitter

BATCH_SIZE = 5000


def run(warehouses: list[str] | None = None, batch_size: int = BATCH_SIZE) -> dict:
	summary = {"created": 0, "skipped": 0}

	for warehouse in warehouses or _all_warehouses():
		cursor = None
		warehouse_created = 0
		while True:
			rows = _next_batch(warehouse, cursor, batch_size)
			if not rows:
				break

			cursor = (rows[-1].posting_datetime, rows[-1].creation, rows[-1].name)
			existing = _existing_event_sles([row.name for row in rows])
			pending = [row for row in rows if row.name not in existing]

			summary["skipped"] += len(rows) - len(pending)
			created = _insert_events(pending)
			summary["created"] += created
			warehouse_created += created
			# each batch is durable so a killed run resumes where it stopped
			frappe.db.commit()

		if warehouse_created:
			print(f"[backfill] {warehouse}: +{warehouse_created} (total {summary['created']})", flush=True)

	return summary


def _insert_events(rows: list[frappe._dict]) -> int:
	"""Insert events preserving legacy order.

	Plain rows go through one bulk INSERT per batch (the doc API is far too
	slow for production-scale history); bundle-carrying rows need allocation
	children, so they use the emitter — flushing the bulk buffer first keeps
	the auto-increment ids in legacy order."""
	buffer: list[dict] = []
	count = 0

	for row in rows:
		if row.serial_and_batch_bundle:
			count += _flush(buffer)
			stock_event_emitter.emit_for_sle(row, source="Backfill")
			count += 1
		else:
			args = stock_event_emitter.event_args_from_sle(row)
			args["source"] = "Backfill"
			buffer.append(args)

	return count + _flush(buffer)


BULK_FIELDS = (
	"name",
	"item_code",
	"warehouse",
	"company",
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
	"source",
	"content_hash",
	"creation",
	"modified",
	"owner",
	"modified_by",
)


def _flush(buffer: list[dict]) -> int:
	if not buffer:
		return 0

	# autoincrement names come from a DB sequence assigned in Python, so a bulk
	# insert must reserve its block explicitly (backfill runs single-writer)
	first = frappe.db.get_next_sequence_val("Stock Event")
	if len(buffer) > 1:
		frappe.db.set_next_sequence_val("Stock Event", first + len(buffer) - 1, is_val_used=True)

	timestamp = frappe.utils.now()
	audit = {
		"creation": timestamp,
		"modified": timestamp,
		"owner": "Administrator",
		"modified_by": "Administrator",
	}
	values = [
		[({**args, **audit, "name": first + index}).get(field) for field in BULK_FIELDS]
		for index, args in enumerate(buffer)
	]
	frappe.db.bulk_insert("Stock Event", BULK_FIELDS, values)

	inserted = len(buffer)
	buffer.clear()
	return inserted


def verify(warehouses: list[str] | None = None, batch_size: int = BATCH_SIZE) -> dict:
	"""Check the backfill gate: legacy order reproduced exactly, hashes deterministic.

	Walks each warehouse's live SLEs in legacy order alongside its events in
	fact order ``(posting_datetime, id)`` per item, and recomputes every
	event's content hash from the SLE row.
	"""
	report = {"checked": 0, "missing": [], "order_mismatches": [], "hash_mismatches": []}

	for warehouse in warehouses or _all_warehouses():
		expected = {}
		cursor = None
		while True:
			rows = _next_batch(warehouse, cursor, batch_size)
			if not rows:
				break

			cursor = (rows[-1].posting_datetime, rows[-1].creation, rows[-1].name)
			events = _events_by_sle([row.name for row in rows])

			for row in rows:
				report["checked"] += 1
				event = events.get(row.name)
				if not event:
					report["missing"].append(row.name)
					continue

				if event.content_hash != stock_event_emitter.event_args_from_sle(row)["content_hash"]:
					report["hash_mismatches"].append(row.name)

				expected.setdefault(row.item_code, []).append(cint(event.name))

		for item_code, event_ids in expected.items():
			if event_ids != sorted(event_ids):
				report["order_mismatches"].append((item_code, warehouse))

	report["ok"] = not (report["missing"] or report["order_mismatches"] or report["hash_mismatches"])
	return report


def _all_warehouses() -> list[str]:
	return frappe.get_all("Warehouse", filters={"is_group": 0}, order_by="name", pluck="name")


def _next_batch(warehouse: str, cursor: tuple | None, batch_size: int) -> list[frappe._dict]:
	sle = frappe.qb.DocType("Stock Ledger Entry")
	query = (
		frappe.qb.from_(sle)
		.select("*")
		.where((sle.warehouse == warehouse) & (sle.is_cancelled == 0))
		.orderby(sle.posting_datetime)
		.orderby(sle.creation)
		.orderby(sle.name)
		.limit(batch_size)
	)

	if cursor:
		posting_datetime, creation, name = cursor
		query = query.where(
			(sle.posting_datetime > posting_datetime)
			| ((sle.posting_datetime == posting_datetime) & (sle.creation > creation))
			| ((sle.posting_datetime == posting_datetime) & (sle.creation == creation) & (sle.name > name))
		)

	return query.run(as_dict=True)


def _existing_event_sles(sle_names: list[str]) -> set[str]:
	return set(frappe.get_all("Stock Event", filters={"sle": ("in", sle_names)}, pluck="sle"))


def _events_by_sle(sle_names: list[str]) -> dict[str, frappe._dict]:
	events = frappe.get_all(
		"Stock Event",
		filters={"sle": ("in", sle_names)},
		fields=["name", "sle", "content_hash"],
	)
	return {event.sle: event for event in events}
