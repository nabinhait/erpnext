"""Immutable stock facts. Everything else in the engine is computed from these."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .lots import Allocation, LotType


class EventKind(str, Enum):
	OPENING = "Opening"
	RECEIPT = "Receipt"
	ISSUE = "Issue"
	ASSERTION = "Assertion"
	REVERSAL = "Reversal"
	REVALUATION = "Revaluation"


@dataclass(frozen=True, slots=True)
class Event:
	"""One stock fact for one key. Total order is (posting_datetime, id).

	qty_change is signed. declared_rate is required inward (Receipt/Opening and
	stock-returning Reversals); outward movements carry no rate — the fold
	computes their cost from prior state. Allocations cover the lot-tracked
	part of the movement; any remainder is unlotted stock and folds against
	the key's shared pool (a batch without batchwise valuation is a quantity
	tag, not a sub-fold — its allocation stays in storage but never reaches
	the engine).
	"""

	id: int
	posting_datetime: datetime
	kind: EventKind
	qty_change: float = 0.0
	declared_rate: float | None = None
	assert_qty: float | None = None
	assert_rate: float | None = None
	reverses_event: int | None = None
	value_change: float = 0.0
	allocations: tuple[Allocation, ...] = ()
	rate_buckets: tuple[tuple[float, float], ...] = ()
	"""(qty, rate) groups an outward movement declared it consumes — serial-wise
	valuation: each picked unit leaves at its own receipt rate. Matching layers
	are consumed per bucket; any remainder falls back to declared_rate/policy."""

	def __post_init__(self) -> None:
		validate_event(self)

	@property
	def sort_key(self) -> tuple[datetime, int]:
		return (self.posting_datetime, self.id)


def validate_event(event: Event) -> None:
	"""Shape errors raise here, at write time. Business states never raise in the fold."""
	kind = event.kind
	if kind in (EventKind.RECEIPT, EventKind.OPENING):
		if event.qty_change <= 0 or event.declared_rate is None:
			raise ValueError(f"{kind.value} needs qty_change > 0 and a declared_rate")
	elif kind is EventKind.ISSUE:
		if event.qty_change >= 0:
			raise ValueError("Issue needs qty_change < 0")
	elif kind is EventKind.ASSERTION:
		if event.assert_qty is None or event.assert_rate is None:
			raise ValueError("Assertion needs assert_qty and assert_rate")
		# a negative assert_qty freezes a legacy uncovered balance as exposure
		if event.assert_qty < 0 and event.allocations:
			raise ValueError("a negative assertion cannot carry lot allocations")
	elif kind is EventKind.REVERSAL:
		if event.reverses_event is None or event.qty_change == 0:
			raise ValueError("Reversal needs reverses_event and a signed qty_change")
		if event.qty_change > 0 and event.declared_rate is None:
			raise ValueError("A stock-returning reversal needs the original consumed rate")
	elif kind is EventKind.REVALUATION:
		if event.reverses_event is None or event.value_change == 0:
			raise ValueError("Revaluation needs reverses_event and a nonzero value_change")
		if event.qty_change != 0:
			raise ValueError("Revaluation moves value, not quantity")
		if event.allocations:
			raise ValueError("Revaluations do not carry allocations")
	if event.allocations:
		validate_allocations(event)
	if event.rate_buckets:
		validate_rate_buckets(event)


def validate_rate_buckets(event: Event) -> None:
	if event.qty_change >= 0:
		raise ValueError("rate_buckets apply to outward movements only")
	total = 0.0
	for qty, rate in event.rate_buckets:
		if qty <= 0 or rate < 0:
			raise ValueError("rate bucket needs qty > 0 and rate >= 0")
		total += qty
	if total > -event.qty_change + 1e-9:
		raise ValueError("rate buckets cannot exceed qty_change")


def validate_allocations(event: Event) -> None:
	total = sum(allocation.qty for allocation in event.allocations)
	if event.kind is EventKind.ASSERTION:
		# assertion allocations assert lot presence and seed sub-states
		if any(allocation.qty <= 0 for allocation in event.allocations):
			raise ValueError("assertion allocations assert presence: qty must be > 0")
		if total > event.assert_qty + 1e-9:
			raise ValueError("assertion allocations cannot exceed assert_qty")
	else:
		if abs(total) > abs(event.qty_change) + 1e-9:
			raise ValueError("allocations cannot exceed qty_change")
		if total * event.qty_change < 0:
			raise ValueError("allocations must move in the event's direction")
	keys = {(a.lot_type, a.lot_id) for a in event.allocations}
	if len(keys) != len(event.allocations):
		raise ValueError("duplicate lot in allocations")
	for allocation in event.allocations:
		if allocation.qty == 0:
			raise ValueError("allocation qty cannot be zero")
		if allocation.lot_type is LotType.SERIAL and allocation.qty not in (-1, 1):
			raise ValueError("serial allocations move exactly one unit")
