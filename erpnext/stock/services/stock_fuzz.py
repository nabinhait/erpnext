# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Concurrent stock fuzzing — rung 2 of the verification ladder.

Seeded worker threads, each with its own database connection, submit
overlapping stock transactions against a shared item/warehouse pool: plain
receipts and issues on a hot key, two-key transfers, and backdated entries
racing live submits. Individual failures (negative stock, deadlocks, lock
timeouts) are expected outcomes and are counted, not raised. Afterwards,
queued Repost Item Valuations are processed synchronously and every touched
key is checked against the ledger invariants:

1. running Σ actual_qty equals qty_after_transaction on every row
2. final stock_value equals Σ stock_value_difference
3. Bin actual_qty and stock_value match the last ledger row
4. with dual-write on: every live SLE has exactly one Stock Event

Writes real (committed) documents — run on a disposable or test site:

    bench --site <site> execute erpnext.stock.services.stock_fuzz.run
"""

import random
import threading

import frappe
from frappe.utils import add_days, flt, nowdate

QTY_TOLERANCE = 1e-3
VALUE_TOLERANCE = 0.5


def run(company: str | None = None, workers: int = 4, iterations: int = 25, seed: int = 42) -> dict:
	site = frappe.local.site
	company = (
		company
		or frappe.defaults.get_global_default("company")
		or frappe.get_all("Company", pluck="name", limit=1)[0]
	)
	fixture = _setup(company, seed)

	outcomes = [[] for _ in range(workers)]
	threads = [
		threading.Thread(
			target=_worker,
			args=(site, fixture, seed + index, iterations, outcomes[index]),
			daemon=True,
		)
		for index in range(workers)
	]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()

	frappe.db.commit()  # leave the pre-fuzz snapshot so the checks see the workers' writes
	_process_pending_reposts(fixture)

	report = _check_invariants(fixture)
	report["operations"] = _tally(outcomes)
	report["fixture"] = {"items": fixture["items"], "warehouses": fixture["warehouses"]}
	return report


def _setup(company: str, seed: int) -> dict:
	tag = frappe.generate_hash(length=6)
	warehouses = [_ensure_warehouse(f"Fuzz WH {tag} {index}", company) for index in (1, 2)]
	items = [_ensure_item(f"FUZZ-{tag}-{index}") for index in (1, 2, 3)]

	from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

	for item in items:
		for warehouse in warehouses:
			make_stock_entry(
				item_code=item,
				target=warehouse,
				qty=500,
				rate=100,
				posting_date=add_days(nowdate(), -10),
				company=company,
			)

	frappe.db.commit()
	return {"company": company, "items": items, "warehouses": warehouses}


def _worker(site: str, fixture: dict, seed: int, iterations: int, outcome: list) -> None:
	frappe.init(site=site)
	frappe.connect()
	frappe.set_user("Administrator")
	rng = random.Random(seed)

	try:
		for _ in range(iterations):
			# the web layer retries deadlocked requests; mimic it so contention
			# shows up as retries, not abandoned operations
			for attempt in range(3):
				try:
					_random_operation(rng, fixture)
					frappe.db.commit()
					outcome.append("ok" if attempt == 0 else "ok_after_retry")
					break
				except frappe.QueryDeadlockError:
					frappe.db.rollback()
					if attempt == 2:
						outcome.append("deadlock_exhausted")
				except Exception as error:
					frappe.db.rollback()
					outcome.append(type(error).__name__)
					break
	finally:
		frappe.destroy()


def _random_operation(rng: random.Random, fixture: dict) -> None:
	from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

	# first item is the hot key: half of all traffic lands on it
	item = fixture["items"][0] if rng.random() < 0.5 else rng.choice(fixture["items"])
	warehouse = rng.choice(fixture["warehouses"])
	other = fixture["warehouses"][1 - fixture["warehouses"].index(warehouse)]
	roll = rng.random()
	backdate = add_days(nowdate(), -rng.randint(1, 3)) if rng.random() < 0.2 else None

	if roll < 0.45:
		make_stock_entry(
			item_code=item,
			target=warehouse,
			qty=rng.randint(1, 20),
			rate=rng.randint(80, 120),
			posting_date=backdate,
			company=fixture["company"],
		)
	elif roll < 0.8:
		make_stock_entry(
			item_code=item,
			source=warehouse,
			qty=rng.randint(1, 10),
			posting_date=backdate,
			company=fixture["company"],
		)
	else:
		make_stock_entry(
			item_code=item,
			source=warehouse,
			target=other,
			qty=rng.randint(1, 10),
			posting_date=backdate,
			company=fixture["company"],
		)


def _process_pending_reposts(fixture: dict) -> None:
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

	pending = frappe.get_all(
		"Repost Item Valuation",
		filters={"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		pluck="name",
	)
	for name in pending:
		repost(frappe.get_doc("Repost Item Valuation", name))

	frappe.db.commit()


def _check_invariants(fixture: dict) -> dict:
	report = {"keys": 0, "rows": 0, "violations": []}
	dual_write = bool(frappe.conf.get("stock_event_dual_write"))

	for item in fixture["items"]:
		for warehouse in fixture["warehouses"]:
			rows = _ledger_rows(item, warehouse)
			if not rows:
				continue

			report["keys"] += 1
			report["rows"] += len(rows)
			key = f"{item} @ {warehouse}"

			running = 0.0
			value_sum = 0.0
			for row in rows:
				running += flt(row.actual_qty)
				value_sum += flt(row.stock_value_difference)
				if abs(running - flt(row.qty_after_transaction)) > QTY_TOLERANCE:
					report["violations"].append((key, row.name, "running qty != qty_after_transaction"))

			last = rows[-1]
			if abs(value_sum - flt(last.stock_value)) > VALUE_TOLERANCE:
				report["violations"].append((key, last.name, "sum(svd) != final stock_value"))

			bin_row = frappe.db.get_value(
				"Bin", {"item_code": item, "warehouse": warehouse}, ["actual_qty", "stock_value"], as_dict=1
			)
			if bin_row:
				if abs(flt(bin_row.actual_qty) - flt(last.qty_after_transaction)) > QTY_TOLERANCE:
					report["violations"].append((key, "Bin", "bin actual_qty != last qty_after"))
				if abs(flt(bin_row.stock_value) - flt(last.stock_value)) > VALUE_TOLERANCE:
					report["violations"].append((key, "Bin", "bin stock_value != last stock_value"))

			if dual_write:
				missing = len(rows) - frappe.db.count(
					"Stock Event", {"sle": ("in", [row.name for row in rows])}
				)
				if missing:
					report["violations"].append((key, "Stock Event", f"{missing} live SLEs without events"))

	report["ok"] = not report["violations"]
	return report


def _ledger_rows(item: str, warehouse: str) -> list[frappe._dict]:
	return frappe.get_all(
		"Stock Ledger Entry",
		filters={"item_code": item, "warehouse": warehouse, "is_cancelled": 0},
		fields=["name", "actual_qty", "qty_after_transaction", "stock_value", "stock_value_difference"],
		order_by="posting_datetime, creation, name",
	)


def _tally(outcomes: list[list[str]]) -> dict:
	tally: dict[str, int] = {}
	for outcome in outcomes:
		for entry in outcome:
			tally[entry] = tally.get(entry, 0) + 1
	return tally


def _ensure_warehouse(name: str, company: str) -> str:
	abbreviation = frappe.get_cached_value("Company", company, "abbr")
	full_name = f"{name} - {abbreviation}"
	if not frappe.db.exists("Warehouse", full_name):
		frappe.get_doc(doctype="Warehouse", warehouse_name=name, company=company).insert(
			ignore_permissions=True
		)
	return full_name


def _ensure_item(item_code: str) -> str:
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc(
			doctype="Item",
			item_code=item_code,
			item_name=item_code,
			item_group="All Item Groups",
			stock_uom="Nos",
			is_stock_item=1,
		).insert(ignore_permissions=True)
	return item_code
