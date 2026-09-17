"""Voucher-level fold: all legs of one voucher in one pass (design doc §2.5).

A voucher's inward legs cannot carry a rate up front — their value is the cost
the outgoing legs gave up, pooled with the voucher's fixed charges and split by
a rule (pooling.py). apply_voucher folds the sources first, realizes a whole
pool at once, then folds its outputs — so coupled legs share one number in
memory and nothing is ever written back into a document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .apply import apply_event
from .context import EngineContext
from .event import Event, EventKind
from .pooling import CostPool, PooledLeg, allocate_pool, pool_value, realize_leg
from .state import EventEffect, State

_KIND_ORDER = {
	EventKind.REVERSAL: 0,
	EventKind.ISSUE: 1,
	EventKind.ASSERTION: 2,
	EventKind.RECEIPT: 3,
	EventKind.OPENING: 3,
	EventKind.REVALUATION: 4,
}


@dataclass(frozen=True, slots=True)
class Leg:
	"""A voucher line carrying a ready Event. `key` is opaque to the core."""

	key: str
	event: Event

	@property
	def id(self) -> int:
		return self.event.id

	@property
	def kind(self) -> EventKind:
		return self.event.kind


VoucherLeg = Leg | PooledLeg


@dataclass(frozen=True, slots=True)
class Voucher:
	"""Plain legs, plus the cost pools whose outputs are valued by the fold."""

	legs: tuple[Leg, ...]
	pools: tuple[CostPool, ...] = ()

	@property
	def pooled_legs(self) -> tuple[PooledLeg, ...]:
		return tuple(output for pool in self.pools for output in pool.outputs)


@dataclass(frozen=True, slots=True)
class VoucherResult:
	"""realized_legs pairs every folded Event with its key, in fold order —
	the facts that would be persisted. effects is parallel to realized_legs."""

	states: dict[str, State]
	effects: tuple[EventEffect, ...]
	realized_legs: tuple[Leg, ...]


def apply_voucher(
	states: Mapping[str, State],
	voucher: Voucher,
	context: EngineContext | Mapping[str, EngineContext],
) -> VoucherResult:
	"""Fold every leg of one voucher against `states` (missing key = State()).

	Legs fold in intra-voucher kind order (Reversal < Issue < Assertion <
	Receipt) then id, deferred where cost must flow forward: a pooled leg folds
	after its pool's sources, and a source folds after pooled inflows to its own
	key from *other* pools — which is what lets a chain A->B->C fold correctly
	while a manufacture may still consume from and produce into one warehouse. A
	cyclic dependency (such as a swap A<->B in one voucher) raises ValueError.
	`context` is one EngineContext for every key, or a mapping covering each key.
	"""
	new_states = dict(states)
	effects: list[EventEffect] = []
	realized: list[Leg] = []
	effects_by_id: dict[int, EventEffect] = {}
	realizer = _PoolRealizer(voucher.pools)
	for leg in _order_legs_by_dependency(voucher):
		event = leg.event if isinstance(leg, Leg) else realizer.realize(leg, effects_by_id)
		state, effect = apply_event(new_states.get(leg.key, State()), event, _context_for(context, leg.key))
		new_states[leg.key] = state
		effects_by_id[event.id] = effect
		effects.append(effect)
		realized.append(Leg(leg.key, event))
	return VoucherResult(new_states, tuple(effects), tuple(realized))


class _PoolRealizer:
	"""Realizes a pool's outputs together — the rule needs every output at once."""

	def __init__(self, pools: tuple[CostPool, ...]) -> None:
		self.pool_of = {output.id: pool for pool in pools for output in pool.outputs}
		self.events: dict[int, Event] = {}

	def realize(self, leg: PooledLeg, effects_by_id: dict[int, EventEffect]) -> Event:
		if leg.id not in self.events:
			self._realize_pool(self.pool_of[leg.id], effects_by_id)
		return self.events[leg.id]

	def _realize_pool(self, pool: CostPool, effects_by_id: dict[int, EventEffect]) -> None:
		values = allocate_pool(pool_value(pool, effects_by_id.__getitem__), pool.outputs, pool.rule)
		for output, value in zip(pool.outputs, values, strict=True):
			self.events[output.id] = realize_leg(output, value)


def _order_legs_by_dependency(voucher: Voucher) -> list[VoucherLeg]:
	"""Base order (kind, id), each leg deferred until its dependencies folded."""
	by_id = _legs_by_id(voucher)
	_validate_pools(voucher, by_id)
	dependencies = _dependencies(voucher, by_id)
	pending = sorted(by_id.values(), key=lambda leg: (_KIND_ORDER[leg.kind], leg.id))
	ordered: list[VoucherLeg] = []
	done: set[int] = set()
	while pending:
		ready = next((leg for leg in pending if dependencies[leg.id] <= done), None)
		if ready is None:
			raise ValueError("cyclic cost dependency within voucher")
		pending.remove(ready)
		done.add(ready.id)
		ordered.append(ready)
	return ordered


def _legs_by_id(voucher: Voucher) -> dict[int, VoucherLeg]:
	legs: tuple[VoucherLeg, ...] = (*voucher.legs, *voucher.pooled_legs)
	by_id = {leg.id: leg for leg in legs}
	if len(by_id) != len(legs):
		raise ValueError("duplicate leg ids in voucher")
	return by_id


def _validate_pools(voucher: Voucher, by_id: dict[int, VoucherLeg]) -> None:
	claimed: set[int] = set()
	for pool in voucher.pools:
		for source_id in pool.sources:
			source = by_id.get(source_id)
			if not isinstance(source, Leg) or source.event.qty_change >= 0:
				raise ValueError(f"source {source_id} must reference an outgoing leg in the same voucher")
			if source_id in claimed:
				raise ValueError(f"leg {source_id} feeds more than one cost pool")
			claimed.add(source_id)


def _dependencies(voucher: Voucher, by_id: dict[int, VoucherLeg]) -> dict[int, set[int]]:
	"""A pooled leg waits for its sources; a source waits for pooled inflows to its
	own key — excluding its own pool's, so consuming and producing in one warehouse
	is an ordinary voucher rather than a cycle."""
	dependencies: dict[int, set[int]] = {leg_id: set() for leg_id in by_id}
	for pool in voucher.pools:
		own_outputs = {output.id for output in pool.outputs}
		for output_id in own_outputs:
			dependencies[output_id] |= set(pool.sources)
		for source_id in pool.sources:
			source_key = by_id[source_id].key
			dependencies[source_id] |= {
				leg.id for leg in voucher.pooled_legs if leg.key == source_key and leg.id not in own_outputs
			}
	return dependencies


def _context_for(context: EngineContext | Mapping[str, EngineContext], key: str) -> EngineContext:
	return context if isinstance(context, EngineContext) else context[key]
