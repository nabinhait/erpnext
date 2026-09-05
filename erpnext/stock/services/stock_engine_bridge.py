# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Bridge between erpnext's Stock Event rows and the stock_engine pure core.

Owns the three translations every fold consumer needs: engine import (the
stock_engine app is an install-time dependency of shadow mode and fold
authority), Stock Event row → engine Event (optionally with lot allocations),
and engine State ↔ JSON for checkpoint persistence.
"""

import frappe
from frappe.utils import cint, flt

from erpnext.stock.utils import get_valuation_method


def engine() -> frappe._dict:
	from erpnext.stock.engine.context import FoldContext
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
		FoldContext=FoldContext,
		Fifo=Fifo,
		Lifo=Lifo,
		MovingAverage=MovingAverage,
		replay=replay,
		Layer=Layer,
		LotState=LotState,
		State=State,
	)


def policy_for(item_code: str, eng: frappe._dict | None = None, honor_serialwise: bool = True):
	"""Engine policy for the item, or None when its method has no fold parity yet.

	Serial-wise items fold layered regardless of their valuation method: each
	unit leaves at its own receipt rate via rate buckets, which need the
	receipt layers kept distinct — the method only matters for their
	non-serialized siblings. History replays (shadow) pass False."""
	eng = eng or engine()
	if honor_serialwise and _serialwise_valuation(item_code):
		return eng.Fifo()
	method = get_valuation_method(item_code)
	if method == "Standard Cost":
		return None
	if method == "LIFO":
		return eng.Lifo()
	if method == "Moving Average":
		return eng.MovingAverage()
	return eng.Fifo()


def to_event(
	eng: frappe._dict,
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
	kind = eng.EventKind(row.kind)
	if row.kind == "Reversal" and not row.reverses_event:
		# best-effort pairing failed; fold it as the movement it is
		kind = eng.EventKind.RECEIPT if flt(row.qty_change) > 0 else eng.EventKind.ISSUE

	if kind is eng.EventKind.ASSERTION and row.sle:
		# a legacy reco resets the whole key; lots reconverge from later facts.
		# An SLE-less assertion is a cutover baseline: its allocations seed lots.
		allocations = None
	if allocations and honor_batch_flag:
		allocations = [a for a in allocations if a.serial_no or _batch_in_valuation(a.batch_no)]
	if allocations:
		# stored rows from before the emitter aligned bundle-sourced signs may
		# oppose the event: the allocation names the lot, the event dictates
		# the direction
		total = sum(flt(a.qty_change) for a in allocations)
		if total * flt(row.qty_change) < 0:
			allocations = [frappe._dict({**a, "qty_change": -flt(a.qty_change)}) for a in allocations]

	rate_buckets = ()
	if allocations and honor_batch_flag and kind is not eng.EventKind.ASSERTION:
		serials = [a for a in allocations if a.serial_no]
		if serials:
			allocations = [a for a in allocations if not a.serial_no]
			if flt(row.qty_change) < 0 and _serialwise_valuation(row.get("item_code")):
				rate_buckets = _rate_buckets(serials)

	return eng.Event(
		id=cint(row.name),
		posting_datetime=row.posting_datetime,
		kind=kind,
		qty_change=flt(row.qty_change),
		declared_rate=flt(row.declared_rate)
		if flt(row.qty_change) > 0
		else (flt(row.declared_rate) or None),
		assert_qty=flt(row.assert_qty) if row.kind == "Assertion" else None,
		assert_rate=flt(row.assert_rate) if row.kind == "Assertion" else None,
		reverses_event=cint(row.reverses_event) or None,
		value_change=flt(row.get("value_change")),
		allocations=tuple(_to_allocation(eng, alloc) for alloc in allocations or []),
		rate_buckets=rate_buckets,
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


def deserialize_state(eng: frappe._dict, data: dict):
	return eng.State(
		layers=tuple(
			eng.Layer(qty=layer[0], rate=layer[1], source_event_id=layer[2]) for layer in data["layers"]
		),
		exposure_qty=data["exposure_qty"],
		exposure_rate=data["exposure_rate"],
		lots=tuple(
			eng.LotState(
				lot_type=eng.LotType(lot["lot_type"]),
				lot_id=lot["lot_id"],
				state=deserialize_state(eng, lot["state"]),
			)
			for lot in data["lots"]
		),
	)


def _batch_in_valuation(batch_no: str | None) -> bool:
	if not batch_no:
		return False
	return bool(frappe.get_cached_value("Batch", batch_no, "use_batchwise_valuation"))


def _serialwise_valuation(item_code: str | None) -> bool:
	if not item_code:
		return False
	return bool(frappe.get_cached_value("Item", item_code, "use_serialwise_valuation"))


def _rate_buckets(serials: list[frappe._dict]) -> tuple[tuple[float, float], ...]:
	"""Group picked serials by their receipt rate; zero-rate rows (no stored
	rate) fall back to the pool."""
	buckets: dict[float, float] = {}
	for alloc in serials:
		rate = flt(alloc.get("declared_rate"))
		if rate > 0:
			buckets[rate] = buckets.get(rate, 0.0) + abs(flt(alloc.qty_change))
	return tuple((qty, rate) for rate, qty in sorted(buckets.items()))


def _to_allocation(eng: frappe._dict, alloc: frappe._dict):
	lot_type = eng.LotType.SERIAL if alloc.serial_no else eng.LotType.BATCH
	return eng.Allocation(
		lot_type=lot_type,
		lot_id=alloc.serial_no or alloc.batch_no,
		qty=flt(alloc.qty_change),
		declared_rate=flt(alloc.get("declared_rate")) or None,
	)
