"""Cross-key cost propagation (design doc §1.9, §2.5).

A backdate refolds its own key. If that refold changes the consumed rate of an
outgoing leg that realized an inward event on another key (a CostLink), the
inward event is re-realized at the new rate and its key refolded from that
point — breadth-first until every link converges. Both convergence cuts hold:
a refold that converges before reaching a linked source never fires the link,
and a reached source whose rate is unchanged within tolerance does not fire.
"""
from __future__ import annotations

from collections import Counter, deque
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, replace

from .context import FoldContext
from .event import Event
from .replay import ReplayResult, refold_after_insert, replay
from .state import Effect, State


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
	"""refolds holds each touched key's refolded suffix (latest values where a
	key refolded more than once) with `final` as its post-propagation state.
	invalidations lists (key, from_event_id) in processing order."""

	refolds: dict[str, ReplayResult]
	re_realized_events: tuple[Event, ...]
	streams: dict[str, list[Event]]
	invalidations: tuple[tuple[str, int], ...]


def propagate(
	streams: Mapping[str, list[Event]],
	links: Collection[CostLink],
	inserted: tuple[str, Event],
	context: FoldContext | Mapping[str, FoldContext],
	*,
	recorded: Mapping[str, ReplayResult] | None = None,
	tolerance: float = 1e-9,
	iteration_cap: int = 1000,
) -> PropagationResult:
	"""Refold the trigger key, then walk fired cost links breadth-first.

	`inserted` is (key, event); the event must already be in its key's stream.
	`recorded` optionally supplies prior replay results per key — the trigger
	key's must come from its stream WITHOUT the inserted event; keys not
	supplied are replayed internally from the given streams. Raises
	RuntimeError past `iteration_cap` total link firings (suspected cycle —
	well-formed voucher links cannot cycle, since a link points forward from
	an outgoing leg to a later inward event on another key).
	"""
	return _Walker(streams, links, inserted, context, recorded, tolerance, iteration_cap).run()


class _Walker:
	def __init__(
		self,
		streams: Mapping[str, list[Event]],
		links: Collection[CostLink],
		inserted: tuple[str, Event],
		context: FoldContext | Mapping[str, FoldContext],
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
		self.refolds: dict[str, ReplayResult] = {}
		self.re_realized: list[Event] = []
		self.invalidations: list[tuple[str, int]] = []
		self.queue: deque[tuple[ReplayResult, dict[int, Effect]]] = deque()
		self.fired_counts: Counter[CostLink] = Counter()

	def run(self) -> PropagationResult:
		prior = self._trigger_prior()
		refold = refold_after_insert(
			self.streams[self.trigger_key], self.trigger_event, prior,
			self._context_for(self.trigger_key))
		self._record(self.trigger_key, self.trigger_event.id, refold, prior)
		while self.queue:
			refold, old_effects = self.queue.popleft()
			for link, new_rate in self._fired_links(refold, old_effects):
				self._fire(link, new_rate)
		return PropagationResult(
			self.refolds, tuple(self.re_realized), self.streams, tuple(self.invalidations))

	def _fired_links(
		self, refold: ReplayResult, old_effects: dict[int, Effect]
	) -> Iterator[tuple[CostLink, float]]:
		for link in self.links:
			effect = refold.effects.get(link.source_event_id)
			if effect is None:
				continue  # cut 1: the refold converged before this source
			if effect.consumed_rate is None:
				raise ValueError(f"link source {link.source_event_id} yielded no consumed_rate")
			old = old_effects.get(link.source_event_id)
			if (
				old is not None and old.consumed_rate is not None
				and abs(effect.consumed_rate - old.consumed_rate) <= self.tolerance
			):
				continue  # cut 2: reached, but the rate did not change
			yield link, effect.consumed_rate

	def _fire(self, link: CostLink, new_rate: float) -> None:
		self._guard(link)
		prior = self._recorded_for(link.target_key)
		event = self._re_realize(link, new_rate)
		refold = refold_after_insert(
			self.streams[link.target_key], event, prior, self._context_for(link.target_key))
		self.re_realized.append(event)
		self._record(link.target_key, event.id, refold, prior)

	def _re_realize(self, link: CostLink, new_rate: float) -> Event:
		stream = self.streams[link.target_key]
		index = _index_of(stream, link.target_event_id)
		old = stream[index]
		rate = (new_rate * link.cost_share_qty + link.extra_cost) / old.qty_change
		stream[index] = replace(old, declared_rate=rate)
		return stream[index]

	def _record(
		self, key: str, from_event_id: int, refold: ReplayResult, prior: ReplayResult
	) -> None:
		self.invalidations.append((key, from_event_id))
		self.recorded[key] = _merge_replays(prior, refold)
		self.refolds[key] = _merge_suffix(self.refolds.get(key), refold, self.recorded[key].final)
		self.queue.append((refold, prior.effects))

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

	def _context_for(self, key: str) -> FoldContext:
		return self.context if isinstance(self.context, FoldContext) else self.context[key]


def _merge_replays(prior: ReplayResult, refold: ReplayResult) -> ReplayResult:
	"""The key's full latest belief: prior states overlaid with the refolded ones."""
	final = prior.final if refold.converged_at is not None else refold.final
	return ReplayResult(
		{**prior.states, **refold.states}, {**prior.effects, **refold.effects}, final)


def _merge_suffix(
	existing: ReplayResult | None, refold: ReplayResult, final: State
) -> ReplayResult:
	states = {**existing.states, **refold.states} if existing else dict(refold.states)
	effects = {**existing.effects, **refold.effects} if existing else dict(refold.effects)
	return ReplayResult(states, effects, final, refold.converged_at, refold.skipped)


def _index_of(stream: list[Event], event_id: int) -> int:
	for index, event in enumerate(stream):
		if event.id == event_id:
			return index
	raise ValueError(f"target event {event_id} not found in its key's stream")
