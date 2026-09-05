"""Pure stock-ledger core: immutable events in, states and effects out.

No Frappe, no database, no I/O — enforced by tests/test_purity.py.
"""
from .context import FoldContext
from .event import Event, EventKind
from .fold import fold
from .lots import Allocation, LotType
from .policies import Fifo, Lifo, MovingAverage, StandardCost, ValuationPolicy
from .propagate import CostLink, PropagationResult, propagate
from .replay import ReplayResult, refold_after_insert, replay, sort_events
from .state import Effect, Layer, LotState, State
from .voucher import CostLinkedLeg, Leg, Voucher, VoucherResult, fold_voucher

__all__ = [
	"Allocation",
	"CostLink",
	"CostLinkedLeg",
	"Effect",
	"Event",
	"EventKind",
	"Fifo",
	"FoldContext",
	"Layer",
	"Leg",
	"Lifo",
	"LotState",
	"LotType",
	"MovingAverage",
	"PropagationResult",
	"ReplayResult",
	"StandardCost",
	"State",
	"ValuationPolicy",
	"Voucher",
	"VoucherResult",
	"fold",
	"fold_voucher",
	"propagate",
	"refold_after_insert",
	"replay",
	"sort_events",
]
