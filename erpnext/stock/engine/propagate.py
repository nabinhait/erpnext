"""Cross-key cost propagation (design doc §1.9, §2.5).

A backdate recomputes its own key. If that recompute changes the consumed rate of an
outgoing leg that realized an inward event on another key (a CostLink), the
inward event is re-realized at the new rate and its key recomputed from that
point — breadth-first until every link converges. Both convergence cuts hold:
a recompute that converges before reaching a linked source never fires the link,
and a reached source whose rate is unchanged within tolerance does not fire.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, replace

from .context import EngineContext
from .event import Event
from .replay import ReplayResult, replay, replay_after_insert
from .state import EventEffect, State


@dataclass(frozen=True, slots=True)
class CostLink:
	"""The persisted fact that `target_event_id` (an inward event on
	`target_key`) was realized from `source_event_id`'s consumed rate:
	declared_rate = (consumed_rate * cost_share_qty + extra_cost) / qty_change.
	"""

	source_event_id: int
	target_key: str
	target_event_id: int
	cost_share_qty: float
	extra_cost: float = 0.0


@dataclass(frozen=True, slots=True)
class PropagationResult:
	"""recomputes holds each touched key's recomputed suffix (latest values where a
	key recomputed more than once) with `final` as its post-propagation state.
	invalidations lists (key, from_event_id) in processing order."""

	recomputes: dict[str, ReplayResult]
	re_realized_events: tuple[Event, ...]
	streams: dict[str, list[Event]]
	invalidations: tuple[tuple[str, int], ...]


def propagate_cost_links(
	streams: Mapping[str, list[Event]],
	links: Collection[CostLink],
	inserted: tuple[str, Event],
	context: EngineContext | Mapping[str, EngineContext],
	*,
	recorded: Mapping[str, ReplayResult] | None = None,
	tolerance: float = 1e-9,
	iteration_cap: int = 1000,
) -> PropagationResult:
	"""Recompute the trigger key, then walk fired cost links breadth-first.

	`inserted` is (key, event); the event must already be in its key's stream.
	`recorded` optionally supplies prior replay results per key — the trigger
	key's must come from its stream WITHOUT the inserted event; keys not
	supplied are replayed internally from the given streams. Raises
	RuntimeError past `iteration_cap` total link firings (suspected cycle —
	well-formed voucher links cannot cycle, since a link points forward from
	an outgoing leg to a later inward event on another key).
	"""
	return _CostLinkWalker(streams, links, inserted, context, recorded, tolerance, iteration_cap).run()


class _CostLinkWalker:
	def __init__(
		self,
		streams: Mapping[str, list[Event]],
		links: Collection[CostLink],
		inserted: tuple[str, Event],
		context: EngineContext | Mapping[str, EngineContext],
		recorded: Mapping[str, ReplayResult] | None,
		tolerance: float,
		iteration_cap: int,
	) -> None:
		self.streams = {key: list(events) for key, events in streams.items()}
		self.links = tuple(links)
		self.trigger_key, self.trigger_event = inserted
		self.context = context
		self.recorded: dict[str, ReplayResult] = dict(recorded) if recorded else {}
		self.tolerance = tolerance
		self.iteration_cap = iteration_cap
		self.recomputes: dict[str, ReplayResult] = {}
		self.re_realized: list[Event] = []
		self.invalidations: list[tuple[str, int]] = []
		self.queue: deque[tuple[ReplayResult, dict[int, EventEffect]]] = deque()
		self.fired_counts: Counter[CostLink] = Counter()

	def run(self) -> PropagationResult:
		prior = self._trigger_prior()
		recompute = replay_after_insert(
			self.streams[self.trigger_key], self.trigger_event, prior, self._context_for(self.trigger_key)
		)
		self._record(self.trigger_key, self.trigger_event.id, recompute, prior)
		while self.queue:
			recompute, old_effects = self.queue.popleft()
			for link, new_rate in self._fired_links(recompute, old_effects):
				self._fire(link, new_rate)
		return PropagationResult(
			self.recomputes, tuple(self.re_realized), self.streams, tuple(self.invalidations)
		)

	def _fired_links(
		self, recompute: ReplayResult, old_effects: dict[int, EventEffect]
	) -> Iterator[tuple[CostLink, float]]:
		for link in self.links:
			effect = recompute.effects.get(link.source_event_id)
			if effect is None:
				continue  # cut 1: the recompute converged before this source
			if effect.consumed_rate is None:
				raise ValueError(f"link source {link.source_event_id} yielded no consumed_rate")
			old = old_effects.get(link.source_event_id)
			if (
				old is not None
				and old.consumed_rate is not None
				and abs(effect.consumed_rate - old.consumed_rate) <= self.tolerance
			):
				continue  # cut 2: reached, but the rate did not change
			yield link, effect.consumed_rate

	def _fire(self, link: CostLink, new_rate: float) -> None:
		self._guard(link)
		prior = self._recorded_for(link.target_key)
		event = self._re_realize(link, new_rate)
		recompute = replay_after_insert(
			self.streams[link.target_key], event, prior, self._context_for(link.target_key)
		)
		self.re_realized.append(event)
		self._record(link.target_key, event.id, recompute, prior)

	def _re_realize(self, link: CostLink, new_rate: float) -> Event:
		stream = self.streams[link.target_key]
		index = _index_of(stream, link.target_event_id)
		old = stream[index]
		rate = (new_rate * link.cost_share_qty + link.extra_cost) / old.qty_change
		stream[index] = replace(old, declared_rate=rate)
		return stream[index]

	def _record(self, key: str, from_event_id: int, recompute: ReplayResult, prior: ReplayResult) -> None:
		self.invalidations.append((key, from_event_id))
		self.recorded[key] = _overlay_recomputed_states(prior, recompute)
		self.recomputes[key] = _merge_recomputed_suffix(
			self.recomputes.get(key), recompute, self.recorded[key].final
		)
		self.queue.append((recompute, prior.effects))

	def _guard(self, link: CostLink) -> None:
		self.fired_counts[link] += 1
		total = sum(self.fired_counts.values())
		if total > self.iteration_cap or self.fired_counts[link] > len(self.links) + 1:
			raise RuntimeError("cost-link propagation did not converge; suspected cycle")

	def _trigger_prior(self) -> ReplayResult:
		if self.trigger_key in self.recorded:
			return self.recorded[self.trigger_key]
		stream = self.streams.get(self.trigger_key, [])
		without = [event for event in stream if event.id != self.trigger_event.id]
		if len(without) == len(stream):
			raise ValueError("inserted event must already be in its key's stream")
		return replay(without, self._context_for(self.trigger_key))

	def _recorded_for(self, key: str) -> ReplayResult:
		if key not in self.recorded:
			self.recorded[key] = replay(self.streams[key], self._context_for(key))
		return self.recorded[key]

	def _context_for(self, key: str) -> EngineContext:
		return self.context if isinstance(self.context, EngineContext) else self.context[key]


def _overlay_recomputed_states(prior: ReplayResult, recompute: ReplayResult) -> ReplayResult:
	"""The key's full latest belief: prior states overlaid with the recomputed ones."""
	final = prior.final if recompute.converged_at is not None else recompute.final
	return ReplayResult({**prior.states, **recompute.states}, {**prior.effects, **recompute.effects}, final)


def _merge_recomputed_suffix(
	existing: ReplayResult | None, recompute: ReplayResult, final: State
) -> ReplayResult:
	states = {**existing.states, **recompute.states} if existing else dict(recompute.states)
	effects = {**existing.effects, **recompute.effects} if existing else dict(recompute.effects)
	return ReplayResult(states, effects, final, recompute.converged_at, recompute.skipped)


def _index_of(stream: list[Event], event_id: int) -> int:
	for index, event in enumerate(stream):
		if event.id == event_id:
			return index
	raise ValueError(f"target event {event_id} not found in its key's stream")
