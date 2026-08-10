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
	try:
		from stock_engine.core.context import FoldContext
		from stock_engine.core.event import Event, EventKind
		from stock_engine.core.lots import Allocation, LotType
		from stock_engine.core.policies import Fifo, Lifo, MovingAverage
		from stock_engine.core.replay import replay
		from stock_engine.core.state import Layer, LotState, State

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
	except ImportError:
		frappe.throw("This feature requires the stock_engine app to be installed on this bench")


def policy_for(item_code: str, eng: frappe._dict | None = None):
	"""Engine policy for the item, or None when its method has no fold parity yet."""
	eng = eng or engine()
	method = get_valuation_method(item_code)
	if method == "Standard Cost":
		return None
	if method == "LIFO":
		return eng.Lifo()
	if method == "Moving Average":
		return eng.MovingAverage()
	return eng.Fifo()


def to_event(eng: frappe._dict, row: frappe._dict, allocations: list[frappe._dict] | None = None):
	"""Convert a Stock Event row (plus optional allocation rows) to an engine Event."""
	kind = eng.EventKind(row.kind)
	if row.kind == "Reversal" and not row.reverses_event:
		# best-effort pairing failed; fold it as the movement it is
		kind = eng.EventKind.RECEIPT if flt(row.qty_change) > 0 else eng.EventKind.ISSUE

	return eng.Event(
		id=cint(row.name),
		posting_datetime=row.posting_datetime,
		kind=kind,
		qty_change=flt(row.qty_change),
		declared_rate=flt(row.declared_rate) if flt(row.qty_change) > 0 else None,
		assert_qty=flt(row.assert_qty) if row.kind == "Assertion" else None,
		assert_rate=flt(row.assert_rate) if row.kind == "Assertion" else None,
		reverses_event=cint(row.reverses_event) or None,
		allocations=tuple(_to_allocation(eng, alloc) for alloc in allocations or []),
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


def _to_allocation(eng: frappe._dict, alloc: frappe._dict):
	lot_type = eng.LotType.SERIAL if alloc.serial_no else eng.LotType.BATCH
	return eng.Allocation(
		lot_type=lot_type,
		lot_id=alloc.serial_no or alloc.batch_no,
		qty=flt(alloc.qty_change),
	)
