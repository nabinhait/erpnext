"""Cross-key cost propagation (design doc §1.9, §2.5).

A backdate recomputes its own key. If that recompute changes what an outgoing
leg gave up, every cost pool fed by that leg is rebuilt and its rule re-run, so
all of the pool's inward legs are re-realized together — never one rescaled in
isolation, which is what keeps a fixed-rate output fixed and lands the whole
difference on the outputs absorbing the residual. Each re-realized output's key
is then recomputed, breadth-first until every pool converges. Both convergence
cuts hold: a recompute that converges before reaching a pool's sources never
fires it, and reached sources whose cost is unchanged within tolerance do not.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, replace

from .context import EngineContext
from .event import Event
from .pooling import CostPool, PooledLeg, allocate_pool, pool_value, pooled_rate
from .replay import ReplayResult, replay, replay_after_insert
from .state import QTY_EPSILON, EventEffect, State


@dataclass(frozen=True, slots=True)
class PropagationResult:
	"""recomputes holds each touched key's recomputed suffix (latest values where a
	key recomputed more than once) with `final` as its post-propagation state.
	invalidations lists (key, from_event_id) in processing order."""

	recomputes: dict[str, ReplayResult]
	re_realized_events: tuple[Event, ...]
	streams: dict[str, list[Event]]
	invalidations: tuple[tuple[str, int], ...]


def propagate_cost_pools(
	streams: Mapping[str, list[Event]],
	pools: Collection[CostPool],
	inserted: tuple[str, Event],
	context: EngineContext | Mapping[str, EngineContext],
	*,
	recorded: Mapping[str, ReplayResult] | None = None,
	tolerance: float = 1e-9,
	iteration_cap: int = 1000,
) -> PropagationResult:
	"""Recompute the trigger key, then walk fired cost pools breadth-first.

	`inserted` is (key, event); the event must already be in its key's stream.
	`recorded` optionally supplies prior replay results per key — the trigger
	key's must come from its stream WITHOUT the inserted event; keys not
	supplied are replayed internally from the given streams. Raises
	RuntimeError past `iteration_cap` total pool firings (suspected cycle —
	well-formed voucher pools cannot cycle, since a pool points forward from
	its outgoing legs to later inward events).
	"""
	return _CostPoolWalker(streams, pools, inserted, context, recorded, tolerance, iteration_cap).run()


class _CostPoolWalker:
	def __init__(
		self,
		streams: Mapping[str, list[Event]],
		pools: Collection[CostPool],
		inserted: tuple[str, Event],
		context: EngineContext | Mapping[str, EngineContext],
		recorded: Mapping[str, ReplayResult] | None,
		tolerance: float,
		iteration_cap: int,
	) -> None:
		self.streams = {key: list(events) for key, events in streams.items()}
		self.pools = tuple(pools)
		self.trigger_key, self.trigger_event = inserted
		self.context = context
		self.recorded: dict[str, ReplayResult] = dict(recorded) if recorded else {}
		self.tolerance = tolerance
		self.iteration_cap = iteration_cap
		self.key_of_event = {event.id: key for key, stream in self.streams.items() for event in stream}
		self.recomputes: dict[str, ReplayResult] = {}
		self.re_realized: list[Event] = []
		self.invalidations: list[tuple[str, int]] = []
		self.queue: deque[tuple[ReplayResult, dict[int, EventEffect]]] = deque()
		self.fired_counts: Counter[CostPool] = Counter()

	def run(self) -> PropagationResult:
		prior = self._trigger_prior()
		recompute = replay_after_insert(
			self.streams[self.trigger_key], self.trigger_event, prior, self._context_for(self.trigger_key)
		)
		self._record(self.trigger_key, self.trigger_event.id, recompute, prior)
		while self.queue:
			recompute, old_effects = self.queue.popleft()
			for pool in self._fired_pools(recompute, old_effects):
				self._fire(pool)
		return PropagationResult(
			self.recomputes, tuple(self.re_realized), self.streams, tuple(self.invalidations)
		)

	def _fired_pools(
		self, recompute: ReplayResult, old_effects: dict[int, EventEffect]
	) -> Iterator[CostPool]:
		for pool in self.pools:
			reached = [source for source in pool.sources if source in recompute.effects]
			if not reached:
				continue  # cut 1: the recompute converged before this pool's sources
			if all(self._unchanged(source, recompute, old_effects) for source in reached):
				continue  # cut 2: reached, but nothing the pool draws on moved
			yield pool

	def _unchanged(self, source: int, recompute: ReplayResult, old_effects: dict[int, EventEffect]) -> bool:
		old = old_effects.get(source)
		if old is None:
			return False
		return abs(recompute.effects[source].value_delta - old.value_delta) <= self.tolerance

	def _fire(self, pool: CostPool) -> None:
		"""Rebuild the pool, re-run its rule, and recompute every key it feeds."""
		self._guard(pool)
		values = allocate_pool(pool_value(pool, self._effect_for), pool.outputs, pool.rule)
		# priors must be read before re-realization: they are what convergence compares against
		priors = {output.key: self._recorded_for(output.key) for output in pool.outputs}
		touched: dict[str, list[Event]] = {}
		for output, value in zip(pool.outputs, values, strict=True):
			event = self._re_realize(output, value)
			self.re_realized.append(event)
			touched.setdefault(output.key, []).append(event)
		for key, events in touched.items():
			earliest = min(events, key=lambda event: event.sort_key)
			recompute = replay_after_insert(self.streams[key], earliest, priors[key], self._context_for(key))
			self._record(key, earliest.id, recompute, priors[key])

	def _re_realize(self, output: PooledLeg, value: float) -> Event:
		stream = self.streams[output.key]
		index = _index_of(stream, output.id)
		old = stream[index]
		if abs(old.qty_change - output.qty_change) > QTY_EPSILON:
			raise ValueError(f"pooled output {output.id} disagrees with its event on quantity")
		stream[index] = replace(old, declared_rate=pooled_rate(output, value))
		return stream[index]

	def _effect_for(self, source: int) -> EventEffect:
		key = self.key_of_event.get(source)
		if key is None:
			raise ValueError(f"pool source {source} is not in any stream")
		return self._recorded_for(key).effects[source]

	def _record(self, key: str, from_event_id: int, recompute: ReplayResult, prior: ReplayResult) -> None:
		self.invalidations.append((key, from_event_id))
		self.recorded[key] = _overlay_recomputed_states(prior, recompute)
		self.recomputes[key] = _merge_recomputed_suffix(
			self.recomputes.get(key), recompute, self.recorded[key].final
		)
		self.queue.append((recompute, prior.effects))

	def _guard(self, pool: CostPool) -> None:
		self.fired_counts[pool] += 1
		total = sum(self.fired_counts.values())
		if total > self.iteration_cap or self.fired_counts[pool] > len(self.pools) + 1:
			raise RuntimeError("cost-pool propagation did not converge; suspected cycle")

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
