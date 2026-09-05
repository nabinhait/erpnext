"""Fold state and effects. qty and value are computed, never stored — they cannot drift.

State memoises its aggregate (qty, value) per instance: the memo is derived from
the frozen fields by one fixed arithmetic path, is excluded from equality and
hashing, and therefore cannot drift or affect convergence detection.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from operator import attrgetter

from .lots import LotType

_SORT_KEY = attrgetter("sort_key")


@dataclass(frozen=True, slots=True)
class Layer:
	"""A surviving cost layer. source_event_id links it to the receipt that created it."""

	qty: float
	rate: float
	source_event_id: int

	@property
	def value(self) -> float:
		return self.qty * self.rate


@dataclass(frozen=True, slots=True)
class State:
	"""Everything the engine knows about one stock key.

	A negative balance is a modelled exposure (qty issued without cover, valued
	at a provisional rate) — never a negative layer. Lot-tracked stock lives in
	`lots` (one nested single-level State per lot, sorted for canonical
	equality); untracked stock lives in the top-level layers.
	"""

	layers: tuple[Layer, ...] = ()
	exposure_qty: float = 0.0
	exposure_rate: float = 0.0
	lots: tuple[LotState, ...] = ()
	_totals: tuple[float, float] | None = field(
		default=None, init=False, repr=False, compare=False)

	@property
	def qty(self) -> float:
		return self._aggregate()[0]

	@property
	def value(self) -> float:
		return self._aggregate()[1]

	@property
	def valuation_rate(self) -> float:
		return self.value / self.qty if self.qty else 0.0

	def lot(self, lot_type: LotType, lot_id: str) -> State:
		index = self.lot_index(lot_type, lot_id)
		return self.lots[index].state if index is not None else State()

	def lot_index(self, lot_type: LotType, lot_id: str) -> int | None:
		"""Binary-search position of a lot in the sorted lots tuple, or None."""
		index = bisect_left(self.lots, (lot_type.value, lot_id), key=_SORT_KEY)
		if index < len(self.lots):
			entry = self.lots[index]
			if entry.lot_type is lot_type and entry.lot_id == lot_id:
				return index
		return None

	def _aggregate(self) -> tuple[float, float]:
		"""Memoised (qty, value); a pure function of the frozen fields."""
		totals = self._totals
		if totals is None:
			qty = sum(layer.qty for layer in self.layers) - self.exposure_qty
			value = sum(layer.value for layer in self.layers)
			value -= self.exposure_qty * self.exposure_rate
			lots_qty, lots_value = 0.0, 0.0
			for entry in self.lots:
				sub = entry.state
				sub_qty, sub_value = sub._totals or sub._aggregate()
				lots_qty += sub_qty
				lots_value += sub_value
			totals = (qty + lots_qty, value + lots_value)
			object.__setattr__(self, "_totals", totals)
		return totals


@dataclass(frozen=True, slots=True)
class LotState:
	"""One lot's sub-state within a stock key.

	sort_key is precomputed from the identifying fields (excluded from equality,
	like any memo) — it is read on every bisect of the sorted lots tuple.
	"""

	lot_type: LotType
	lot_id: str
	state: State
	sort_key: tuple[str, str] = field(default=("", ""), init=False, repr=False, compare=False)

	def __post_init__(self) -> None:
		object.__setattr__(self, "sort_key", (self.lot_type.value, self.lot_id))


@dataclass(frozen=True, slots=True)
class Effect:
	"""What one event did. Reported to the caller (GL, projections), never fed back in."""

	event_id: int
	qty_after: float
	value_after: float
	value_delta: float
	consumed_rate: float | None = None
	negative: bool = False
	true_up: float = 0.0
	variance: float = 0.0


def validate_state(state: State, deep: bool = True) -> None:
	"""Structural invariants. Business states (like exposure) are legal; corruption is not.

	deep=True (the default, used by tests and external callers) re-validates
	every lot sub-state. The fold hot path passes deep=False and separately
	validates only the lots the current event touched (validate_lot) — untouched
	lots are immutable and were validated when they last changed.
	"""
	assert all(layer.qty > 0 for layer in state.layers), "layers must hold positive qty"
	assert state.exposure_qty >= 0, "exposure cannot be negative"
	assert not (state.layers and state.exposure_qty), "cannot hold stock and exposure at once"
	if not deep:
		return
	keys = [lot.sort_key for lot in state.lots]
	assert keys == sorted(set(keys)), "lots must be unique and sorted"
	for lot in state.lots:
		validate_lot(lot)


def validate_lot(lot: LotState) -> None:
	"""One lot sub-state's invariants: no nesting, sound layers, serial unit bound."""
	assert not lot.state.lots, "lot states cannot nest"
	validate_state(lot.state, deep=False)
	if lot.lot_type is LotType.SERIAL:
		assert -1 <= lot.state.qty <= 1, f"serial {lot.lot_id} must hold one unit at most"
