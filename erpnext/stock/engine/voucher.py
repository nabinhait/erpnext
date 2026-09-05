"""Voucher-level fold: all legs of one voucher in one pass (design doc §2.5).

A transfer's receiving leg cannot carry a rate up front — its rate IS the
outgoing leg's realized cost, which only the fold can compute. CostLinkedLeg
models that leg as a spec; fold_voucher realizes it into an ordinary Event
once its source has folded, so coupled legs share one number in memory and
nothing is ever written back into a document.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .context import FoldContext
from .event import Event, EventKind
from .fold import fold
from .lots import Allocation
from .state import Effect, State

_KIND_ORDER = {
	EventKind.REVERSAL: 0,
	EventKind.ISSUE: 1,
	EventKind.ASSERTION: 2,
	EventKind.RECEIPT: 3,
	EventKind.OPENING: 3,
}
_SHARE_TOLERANCE = 1e-9


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


@dataclass(frozen=True, slots=True)
class CostLinkedLeg:
	"""An inward leg whose rate is the realized cost of `cost_from` — an
	outgoing plain leg in the same voucher.

	cost_share_qty is how many source units' cost this leg absorbs (defaults
	to its own qty; a repack producing 25 from 50 consumed absorbs all 50).
	extra_cost is an absolute amount (operating / landed cost) added on top:
	declared_rate = (source_rate * cost_share_qty + extra_cost) / qty_change.
	"""

	key: str
	id: int
	posting_datetime: datetime
	qty_change: float
	cost_from: int
	extra_cost: float = 0.0
	cost_share_qty: float | None = None
	allocations: tuple[Allocation, ...] = ()

	def __post_init__(self) -> None:
		if self.qty_change <= 0:
			raise ValueError("a cost-linked leg is inward: qty_change must be > 0")
		if self.cost_share_qty is not None and self.cost_share_qty <= 0:
			raise ValueError("cost_share_qty must be > 0")

	@property
	def kind(self) -> EventKind:
		return EventKind.RECEIPT

	@property
	def share_qty(self) -> float:
		return self.cost_share_qty if self.cost_share_qty is not None else self.qty_change


VoucherLeg = Leg | CostLinkedLeg


@dataclass(frozen=True, slots=True)
class Voucher:
	legs: tuple[VoucherLeg, ...]


@dataclass(frozen=True, slots=True)
class VoucherResult:
	"""realized_legs pairs every folded Event with its key, in fold order —
	the facts that would be persisted. effects is parallel to realized_legs."""

	states: dict[str, State]
	effects: tuple[Effect, ...]
	realized_legs: tuple[Leg, ...]


def fold_voucher(
	states: Mapping[str, State],
	voucher: Voucher,
	context: FoldContext | Mapping[str, FoldContext],
) -> VoucherResult:
	"""Fold every leg of one voucher against `states` (missing key = State()).

	Legs fold in intra-voucher kind order (Reversal < Issue < Assertion <
	Receipt) then id, deferred where cost must flow forward: a cost-linked leg
	folds after its source, and a source folds after cost-linked inflows to
	its own key — which is what lets a chain A->B->C fold correctly. A cyclic
	dependency (such as a swap A<->B in one voucher) raises ValueError.
	`context` is one FoldContext for every key, or a mapping covering each key.
	"""
	new_states = dict(states)
	effects: list[Effect] = []
	realized: list[Leg] = []
	effects_by_id: dict[int, Effect] = {}
	for leg in _ordered_legs(voucher):
		event = leg.event if isinstance(leg, Leg) else _realize(leg, effects_by_id)
		state, effect = fold(new_states.get(leg.key, State()), event, _context_for(context, leg.key))
		new_states[leg.key] = state
		effects_by_id[event.id] = effect
		effects.append(effect)
		realized.append(Leg(leg.key, event))
	return VoucherResult(new_states, tuple(effects), tuple(realized))


def _realize(leg: CostLinkedLeg, effects_by_id: dict[int, Effect]) -> Event:
	consumed_rate = effects_by_id[leg.cost_from].consumed_rate
	if consumed_rate is None:
		raise ValueError(f"source leg {leg.cost_from} yielded no consumed_rate")
	declared_rate = (consumed_rate * leg.share_qty + leg.extra_cost) / leg.qty_change
	return Event(
		leg.id, leg.posting_datetime, EventKind.RECEIPT,
		qty_change=leg.qty_change, declared_rate=declared_rate, allocations=leg.allocations,
	)


def _ordered_legs(voucher: Voucher) -> list[VoucherLeg]:
	"""Base order (kind, id), each leg deferred until its dependencies folded."""
	by_id = _legs_by_id(voucher)
	_validate_links(voucher, by_id)
	dependencies = _dependencies(voucher, by_id)
	pending = sorted(voucher.legs, key=lambda leg: (_KIND_ORDER[leg.kind], leg.id))
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
	by_id = {leg.id: leg for leg in voucher.legs}
	if len(by_id) != len(voucher.legs):
		raise ValueError("duplicate leg ids in voucher")
	return by_id


def _validate_links(voucher: Voucher, by_id: dict[int, VoucherLeg]) -> None:
	claimed: dict[int, float] = {}
	for leg in voucher.legs:
		if not isinstance(leg, CostLinkedLeg):
			continue
		source = by_id.get(leg.cost_from)
		if not isinstance(source, Leg) or source.event.qty_change >= 0:
			raise ValueError(
				f"cost_from={leg.cost_from} must reference an outgoing leg in the same voucher")
		claimed[leg.cost_from] = claimed.get(leg.cost_from, 0.0) + leg.share_qty
	for source_id, share in claimed.items():
		if share > -by_id[source_id].event.qty_change + _SHARE_TOLERANCE:
			raise ValueError(f"cost shares against leg {source_id} exceed its outgoing qty")


def _dependencies(voucher: Voucher, by_id: dict[int, VoucherLeg]) -> dict[int, set[int]]:
	dependencies: dict[int, set[int]] = {leg.id: set() for leg in voucher.legs}
	linked = [leg for leg in voucher.legs if isinstance(leg, CostLinkedLeg)]
	for leg in linked:
		dependencies[leg.id].add(leg.cost_from)
	for source_id in {leg.cost_from for leg in linked}:
		source_key = by_id[source_id].key
		dependencies[source_id] |= {leg.id for leg in linked if leg.key == source_key}
	return dependencies


def _context_for(context: FoldContext | Mapping[str, FoldContext], key: str) -> FoldContext:
	return context if isinstance(context, FoldContext) else context[key]
