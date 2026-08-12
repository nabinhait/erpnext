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
			if not frappe.in_test:
				frappe.db.commit()

		if warehouse_created:
			print(f"[backfill] {warehouse}: +{warehouse_created} (total {summary['created']})", flush=True)

	return summary


def _insert_events(rows: list[frappe._dict]) -> int:
	"""Bulk-insert one batch of events (and their allocation children),
	preserving legacy order via an explicitly reserved autoincrement block.
	Live rows are never Reversals, so no pairing lookups are needed."""
	if not rows:
		return 0

	bundle_entries = _bundle_entries([row.serial_and_batch_bundle for row in rows])
	buffer = []
	for row in rows:
		args = stock_event_emitter.event_args_from_sle(
			row,
			allocations=bundle_entries.get(row.serial_and_batch_bundle)
			if row.serial_and_batch_bundle
			else None,
		)
		args["source"] = "Backfill"
		buffer.append(args)

	return _flush(buffer)


def _bundle_entries(bundles: list[str | None]) -> dict[str, list[dict]]:
	names = [bundle for bundle in bundles if bundle]
	if not names:
		return {}

	rows = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": ("in", names)},
		fields=["parent", "serial_no", "batch_no", "qty"],
		order_by="parent, idx",
	)
	grouped: dict[str, list[dict]] = {}
	for row in rows:
		grouped.setdefault(row.parent, []).append(
			{"serial_no": row.serial_no, "batch_no": row.batch_no, "qty_change": row.qty}
		)
	return grouped


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
	values = []
	allocation_values = []
	for index, args in enumerate(buffer):
		event_id = first + index
		values.append([({**args, **audit, "name": event_id}).get(field) for field in BULK_FIELDS])
		for position, allocation in enumerate(args.get("allocations") or [], start=1):
			allocation_values.append(
				[
					frappe.generate_hash(length=10),
					str(event_id),
					"Stock Event",
					"allocations",
					position,
					allocation.get("serial_no"),
					allocation.get("batch_no"),
					allocation.get("qty_change"),
					timestamp,
					timestamp,
					"Administrator",
					"Administrator",
				]
			)

	frappe.db.bulk_insert("Stock Event", BULK_FIELDS, values)
	if allocation_values:
		frappe.db.bulk_insert("Stock Event Allocation", ALLOCATION_FIELDS, allocation_values)

	inserted = len(buffer)
	buffer.clear()
	return inserted


ALLOCATION_FIELDS = (
	"name",
	"parent",
	"parenttype",
	"parentfield",
	"idx",
	"serial_no",
	"batch_no",
	"qty_change",
	"creation",
	"modified",
	"owner",
	"modified_by",
)


def verify(
	warehouses: list[str] | None = None,
	batch_size: int = BATCH_SIZE,
	shard: int | None = None,
	shards: int | None = None,
) -> dict:
	"""Check the backfill gate: legacy order reproduced exactly, hashes deterministic.

	Walks each warehouse's live SLEs in legacy order alongside its events in
	fact order ``(posting_datetime, id)`` per item, and recomputes every
	event's content hash from the SLE row. Pass ``shard``/``shards`` to
	partition the warehouse list for parallel runs.
	"""
	report = {"checked": 0, "missing": [], "order_mismatches": [], "hash_mismatches": []}

	targets = warehouses or _all_warehouses()
	if shards:
		targets = targets[shard::shards]

	for index, warehouse in enumerate(targets):
		if index and index % 500 == 0:
			print(f"[verify] {index}/{len(targets)} warehouses, {report['checked']} rows", flush=True)
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


def verify_fast(hash_sample_size: int = 20000) -> dict:
	"""The verify gate at production scale: set-based SQL for the missing and
	order checks, a random sample for hash determinism.

	Order check: within each (item_code, warehouse), walking SLEs in the
	legacy total order must yield strictly ascending event ids — one window
	function query instead of a per-warehouse row walk."""
	sle = frappe.qb.DocType("Stock Ledger Entry")
	event = frappe.qb.DocType("Stock Event")

	missing = (
		frappe.qb.from_(sle)
		.left_join(event)
		.on(event.sle == sle.name)
		.select(frappe.query_builder.functions.Count("*"))
		.where((sle.is_cancelled == 0) & (event.name.isnull()))
	).run()[0][0]

	order_violations = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT e.name,
				LAG(e.name) OVER (
					PARTITION BY e.item_code, e.warehouse
					ORDER BY s.posting_datetime, s.creation, s.name
				) AS prev
			FROM `tabStock Event` e
			JOIN `tabStock Ledger Entry` s ON e.sle = s.name
			WHERE s.is_cancelled = 0
		) ordered
		WHERE ordered.prev IS NOT NULL AND ordered.prev > ordered.name
		"""
	)[0][0]

	sample = frappe.db.sql(
		"""
		SELECT s.*, e.content_hash AS _event_hash
		FROM `tabStock Event` e
		JOIN `tabStock Ledger Entry` s ON e.sle = s.name
		WHERE s.is_cancelled = 0
		AND e.name %% 31 = 0
		LIMIT %s
		""",
		hash_sample_size,
		as_dict=True,
	)
	hash_mismatches = sum(
		1
		for row in sample
		if row._event_hash != stock_event_emitter.event_args_from_sle(row, allocations=[])["content_hash"]
	)

	return {
		"missing": missing,
		"order_violations": order_violations,
		"hash_sample": len(sample),
		"hash_mismatches": hash_mismatches,
		"ok": not (missing or order_violations or hash_mismatches),
	}
