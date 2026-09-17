"""Cost pooling: what a voucher's inward legs share, and how it is split.

An inward leg is not valued on its own. Every outgoing leg of the voucher gives
up cost, the voucher's fixed charges (labour, power, freight) are added, and the
sum is one pool that a rule splits across the inward legs.

Rules are pure and stateless, so a backdate re-runs the rule on the rebuilt pool
instead of rescaling the answer it gave last time. That is what keeps a
fixed-rate output (scrap) fixed and lands the whole difference on the outputs
that absorb the residual — the recipe is stored, not the rates it produced.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .event import Event, EventKind
from .lots import Allocation
from .state import EventEffect

VALUE_EPSILON = 1e-6
"""Allocated values within this of the pool conserve it: float dust from splitting
by weight never reads as value created or destroyed."""


@dataclass(frozen=True, slots=True)
class PooledLeg:
	"""An inward leg valued from a cost pool instead of a declared rate.

	`fixed_rate` takes that leg out of the split at its own rate — scrap and
	costed-out outputs, which must not move when an input's cost does. The legs
	without one share whatever is left, in proportion to `weight` (defaulting to
	quantity, so an unweighted pool splits by qty and a lone output takes it all).
	"""

	key: str
	id: int
	posting_datetime: datetime
	qty_change: float
	fixed_rate: float | None = None
	weight: float | None = None
	allocations: tuple[Allocation, ...] = ()

	def __post_init__(self) -> None:
		if self.qty_change <= 0:
			raise ValueError("a pooled leg is inward: qty_change must be > 0")
		if self.weight is not None and self.weight < 0:
			raise ValueError("weight cannot be negative")

	@property
	def kind(self) -> EventKind:
		return EventKind.RECEIPT


class CostAllocation(ABC):
	"""Strategy deciding what each inward leg draws from a pool.

	Implementations must be pure and conserving — `allocate_pool` holds them to
	it. Like valuation policies, a custom rule is resolved before the fold starts
	and may not read the database.
	"""

	@abstractmethod
	def allocate(self, pool: float, outputs: Sequence[PooledLeg]) -> tuple[float, ...]:
		"""Value for each output, positionally. Must sum to `pool`."""


class ResidualByWeight(CostAllocation):
	"""Fixed-rate outputs take their own value; the rest split what is left by weight.

	One rule covers every allocation erpnext performs today: a transfer (a lone
	output takes the pool), a repack or manufacture carrying conversion costs,
	scrap held at its own rate while the finished goods absorb the residual, and
	an unpack whose outputs carry declared weights.
	"""

	def allocate(self, pool: float, outputs: Sequence[PooledLeg]) -> tuple[float, ...]:
		fixed = tuple(_fixed_value(output) for output in outputs)
		weights = tuple(
			0.0 if value is not None else _weight(output)
			for output, value in zip(outputs, fixed, strict=True)
		)
		residual = pool - sum(value for value in fixed if value is not None)
		total = sum(weights)
		if not total:
			if abs(residual) > VALUE_EPSILON:
				raise ValueError("no output absorbs the residual: every output holds a fixed rate")
			return tuple(value or 0.0 for value in fixed)
		return tuple(
			value if value is not None else residual * weight / total
			for value, weight in zip(fixed, weights, strict=True)
		)


DEFAULT_ALLOCATION = ResidualByWeight()


@dataclass(frozen=True, slots=True)
class CostPool:
	"""The cost a voucher's inward legs share, and the rule that splits it.

	`sources` are outgoing legs whose whole consumed cost enters the pool.
	`extra_cost` is absolute — it does not move when a source's rate does. The
	same record is the fact persisted for a backdate to replay.
	"""

	sources: tuple[int, ...]
	outputs: tuple[PooledLeg, ...]
	extra_cost: float = 0.0
	rule: CostAllocation = DEFAULT_ALLOCATION

	def __post_init__(self) -> None:
		if not self.outputs:
			raise ValueError("a cost pool needs at least one output")
		if len(set(self.sources)) != len(self.sources):
			raise ValueError("duplicate source in cost pool")


def allocate_pool(pool: float, outputs: Sequence[PooledLeg], rule: CostAllocation) -> tuple[float, ...]:
	"""Run `rule`, holding it to the invariant: what comes out equals what went in."""
	values = rule.allocate(pool, outputs)
	if len(values) != len(outputs):
		raise ValueError(f"{type(rule).__name__} returned {len(values)} values for {len(outputs)} outputs")
	if abs(sum(values) - pool) > VALUE_EPSILON:
		raise ValueError(f"{type(rule).__name__} allocated {sum(values)} out of a pool of {pool}")
	return values


def pool_value(pool: CostPool, effect_for: Callable[[int], EventEffect]) -> float:
	"""What the outputs share: the cost the sources gave up, plus the fixed charges."""
	return sum(-effect_for(source).value_delta for source in pool.sources) + pool.extra_cost


def realize_leg(leg: PooledLeg, value: float) -> Event:
	"""The ordinary Event a pooled leg becomes once its share of the pool is known."""
	return Event(
		leg.id,
		leg.posting_datetime,
		EventKind.RECEIPT,
		qty_change=leg.qty_change,
		declared_rate=pooled_rate(leg, value),
		allocations=leg.allocations,
	)


def pooled_rate(leg: PooledLeg, value: float) -> float:
	return value / leg.qty_change


def _fixed_value(output: PooledLeg) -> float | None:
	return None if output.fixed_rate is None else output.fixed_rate * output.qty_change


def _weight(output: PooledLeg) -> float:
	return output.qty_change if output.weight is None else output.weight
