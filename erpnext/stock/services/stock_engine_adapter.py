# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Bridge between erpnext's Stock Event rows and the stock_engine pure core.

Owns the three translations every fold consumer needs: engine import (the
stock_engine app is an install-time dependency of shadow mode and fold
authority), Stock Event row → engine Event (optionally with lot allocations),
and engine State ↔ JSON for snapshot persistence.
"""

import frappe
from frappe.utils import cint, flt

from erpnext.stock.utils import get_valuation_method


def get_engine() -> frappe._dict:
	from erpnext.stock.engine.context import EngineContext
	from erpnext.stock.engine.event import Event, EventKind
	from erpnext.stock.engine.lots import Allocation, LotType
	from erpnext.stock.engine.policies import Fifo, Lifo, MovingAverage
	from erpnext.stock.engine.replay import replay
	from erpnext.stock.engine.state import Layer, LotState, State

	return frappe._dict(
		Event=Event,
		EventKind=EventKind,
		Allocation=Allocation,
		LotType=LotType,
		EngineContext=EngineContext,
		Fifo=Fifo,
		Lifo=Lifo,
		MovingAverage=MovingAverage,
		replay=replay,
		Layer=Layer,
		LotState=LotState,
		State=State,
	)


def get_valuation_policy(item_code: str, engine: frappe._dict | None = None, honor_serialwise: bool = True):
	"""Engine policy for the item, or None when its method has no fold parity yet.

	Serial-wise items fold layered regardless of their valuation method: each
	unit leaves at its own receipt rate via rate buckets, which need the
	receipt layers kept distinct — the method only matters for their
	non-serialized siblings. History replays (shadow) pass False."""
	engine = engine or get_engine()
	if honor_serialwise and _uses_serialwise_valuation(item_code):
		return engine.Fifo()
	method = get_valuation_method(item_code)
	if method == "Standard Cost":
		return None
	if method == "LIFO":
		return engine.Lifo()
	if method == "Moving Average":
		return engine.MovingAverage()
	return engine.Fifo()


def make_engine_event(
	engine: frappe._dict,
	row: frappe._dict,
	allocations: list[frappe._dict] | None = None,
	honor_batch_flag: bool = True,
):
	"""Convert a Stock Event row (plus optional allocation rows) to an engine Event.

	With honor_batch_flag (the forward semantics): batches without
	use_batchwise_valuation are quantity tags folding against the shared
	pool; serials never become lot sub-states at all — a serial-wise item's
	outward picks turn into rate buckets (each unit leaves at its own
	receipt rate), everything else rides the pool. Shadow and restatement
	trials pass False to replay history's own shape."""
	kind = engine.EventKind(row.kind)
	if row.kind == "Reversal" and not row.reverses_event:
		# best-effort pairing failed; fold it as the movement it is
		kind = engine.EventKind.RECEIPT if flt(row.qty_change) > 0 else engine.EventKind.ISSUE

	if kind is engine.EventKind.ASSERTION and row.sle:
		# a legacy reco resets the whole key; lots reconverge from later facts.
		# An SLE-less assertion is a cutover opening_assertion: its allocations seed lots.
		allocations = None
	if allocations and honor_batch_flag:
		allocations = [a for a in allocations if a.serial_no or _is_batch_in_valuation(a.batch_no)]
	if allocations:
		# stored rows from before the event writer aligned bundle-sourced signs may
		# oppose the event: the allocation names the lot, the event dictates
		# the direction
		total = sum(flt(a.qty_change) for a in allocations)
		if total * flt(row.qty_change) < 0:
			allocations = [frappe._dict({**a, "qty_change": -flt(a.qty_change)}) for a in allocations]

	rate_buckets = ()
	if allocations and honor_batch_flag and kind is not engine.EventKind.ASSERTION:
		serials = [a for a in allocations if a.serial_no]
		if serials:
			allocations = [a for a in allocations if not a.serial_no]
			if flt(row.qty_change) < 0 and _uses_serialwise_valuation(row.get("item_code")):
				rate_buckets = _get_serial_rate_buckets(serials)

	return engine.Event(
		id=cint(row.name),
		posting_datetime=row.posting_datetime,
		kind=kind,
		qty_change=flt(row.qty_change),
		declared_rate=flt(row.declared_rate) if flt(row.qty_change) > 0 else (flt(row.declared_rate) or None),
		assert_qty=flt(row.assert_qty) if row.kind == "Assertion" else None,
		assert_rate=flt(row.assert_rate) if row.kind == "Assertion" else None,
		reverses_event=cint(row.reverses_event) or None,
		value_change=flt(row.get("value_change")),
		allocations=tuple(_make_engine_allocation(engine, allocation) for allocation in allocations or []),
		rate_buckets=rate_buckets,
	)


def get_end_of_day(date) -> str:
	"""The last instant of a date in the fold's total order — where closings,
	snapshots and frontiers sit."""
	return f"{date} 23:59:59.999999"


EVENT_FIELDS = (
	"name",
	"item_code",
	"warehouse",
	"posting_datetime",
	"kind",
	"qty_change",
	"declared_rate",
	"assert_qty",
	"assert_rate",
	"reverses_event",
	"value_change",
	"sle",
	"voucher_type",
	"voucher_no",
)


def make_engine_events(engine: frappe._dict, rows: list[frappe._dict]) -> list:
	"""Engine events for Stock Event rows, in the given order.

	Lot allocations reach the engine only for bundle-backed ledger rows and
	cutover opening_assertions; field-derived lot facts on pre-bundle rows were valued
	aggregate by legacy and stay that way."""
	bundle_rows = get_bundle_backed_sle_names({row.sle for row in rows if row.sle})
	allocations = get_allocations_by_event([row.name for row in rows])
	return [
		make_engine_event(
			engine,
			row,
			allocations.get(str(row.name)) if row.sle in bundle_rows or is_opening_assertion(row) else None,
		)
		for row in rows
	]


def is_opening_assertion(row: frappe._dict) -> bool:
	"""An SLE-less assertion is a cutover opening_assertion; its allocations seed lots."""
	return row.kind == "Assertion" and not row.sle


def get_allocations_by_event(event_names: list) -> dict[str, list[frappe._dict]]:
	if not event_names:
		return {}
	rows = frappe.get_all(
		"Stock Event Allocation",
		filters={"parent": ("in", [str(name) for name in event_names])},
		fields=["parent", "serial_no", "batch_no", "qty_change", "declared_rate"],
		order_by="idx",
	)
	grouped: dict[str, list[frappe._dict]] = {}
	for row in rows:
		grouped.setdefault(str(row.parent), []).append(row)
	return grouped


def get_bundle_backed_sle_names(sle_names: set) -> set[str]:
	if not sle_names:
		return set()
	return set(
		frappe.get_all(
			"Stock Ledger Entry",
			filters={"name": ("in", list(sle_names)), "serial_and_batch_bundle": ("is", "set")},
			pluck="name",
		)
	)


def serialize_state(state) -> dict:
	return {
		"layers": [[layer.qty, layer.rate, layer.source_event_id] for layer in state.layers],
		"exposure_qty": state.exposure_qty,
		"exposure_rate": state.exposure_rate,
		"lots": [
			{
				"lot_type": lot.lot_type.value,
				"lot_id": lot.lot_id,
				"state": serialize_state(lot.state),
			}
			for lot in state.lots
		],
	}


def deserialize_state(engine: frappe._dict, data: dict):
	return engine.State(
		layers=tuple(
			engine.Layer(qty=layer[0], rate=layer[1], source_event_id=layer[2]) for layer in data["layers"]
		),
		exposure_qty=data["exposure_qty"],
		exposure_rate=data["exposure_rate"],
		lots=tuple(
			engine.LotState(
				lot_type=engine.LotType(lot["lot_type"]),
				lot_id=lot["lot_id"],
				state=deserialize_state(engine, lot["state"]),
			)
			for lot in data["lots"]
		),
	)


def _is_batch_in_valuation(batch_no: str | None) -> bool:
	if not batch_no:
		return False
	return bool(frappe.get_cached_value("Batch", batch_no, "use_batchwise_valuation"))


def _uses_serialwise_valuation(item_code: str | None) -> bool:
	if not item_code:
		return False
	return bool(frappe.get_cached_value("Item", item_code, "use_serialwise_valuation"))


def _get_serial_rate_buckets(serials: list[frappe._dict]) -> tuple[tuple[float, float], ...]:
	"""Group picked serials by their receipt rate; zero-rate rows (no stored
	rate) fall back to the pool."""
	buckets: dict[float, float] = {}
	for allocation in serials:
		rate = flt(allocation.get("declared_rate"))
		if rate > 0:
			buckets[rate] = buckets.get(rate, 0.0) + abs(flt(allocation.qty_change))
	return tuple((qty, rate) for rate, qty in sorted(buckets.items()))


def _make_engine_allocation(engine: frappe._dict, allocation: frappe._dict):
	lot_type = engine.LotType.SERIAL if allocation.serial_no else engine.LotType.BATCH
	return engine.Allocation(
		lot_type=lot_type,
		lot_id=allocation.serial_no or allocation.batch_no,
		qty=flt(allocation.qty_change),
		declared_rate=flt(allocation.get("declared_rate")) or None,
	)
