"""Pure stock-ledger core: immutable events in, states and effects out.

No Frappe, no database, no I/O — enforced by tests/test_purity.py.
"""

from .apply import apply_event
from .context import EngineContext
from .event import Event, EventKind
from .lots import Allocation, LotType
from .policies import Fifo, Lifo, MovingAverage, StandardCost, ValuationPolicy
from .pooling import (
	CostAllocation,
	CostPool,
	PooledLeg,
	ResidualByWeight,
	allocate_pool,
	pool_value,
)
from .propagate import PropagationResult, propagate_cost_pools
from .replay import ReplayResult, replay, replay_after_insert, sort_events
from .state import EventEffect, Layer, LotState, State
from .voucher import Leg, Voucher, VoucherResult, apply_voucher

__all__ = [
	"Allocation",
	"CostAllocation",
	"CostPool",
	"EngineContext",
	"Event",
	"EventEffect",
	"EventKind",
	"Fifo",
	"Layer",
	"Leg",
	"Lifo",
	"LotState",
	"LotType",
	"MovingAverage",
	"PooledLeg",
	"PropagationResult",
	"ReplayResult",
	"ResidualByWeight",
	"StandardCost",
	"State",
	"ValuationPolicy",
	"Voucher",
	"VoucherResult",
	"allocate_pool",
	"apply_event",
	"apply_voucher",
	"pool_value",
	"propagate_cost_pools",
	"replay",
	"replay_after_insert",
	"sort_events",
]
