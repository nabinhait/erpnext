"""Ordering, replay, snapshot resume, and convergence detection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .apply import apply_event
from .context import EngineContext
from .event import Event
from .state import EventEffect, State


@dataclass(frozen=True, slots=True)
class ReplayResult:
	states: dict[int, State]
	effects: dict[int, EventEffect]
	final: State
	converged_at: int | None = None
	skipped: tuple[int, ...] = ()


def replay(
	events: Iterable[Event],
	context: EngineContext,
	*,
	start: State | None = None,
	recorded: dict[int, State] | None = None,
) -> ReplayResult:
	"""Fold events in (posting_datetime, id) order from `start`.

	With `recorded` states from a previous fold, stops at the first event whose
	recomputed state equals its recorded one — every later state is then
	provably identical (convergence detection).
	"""
	states: dict[int, State] = {}
	effects: dict[int, EventEffect] = {}
	state = start if start is not None else State()
	ordered = sort_events(events)
	for index, event in enumerate(ordered):
		state, effect = apply_event(state, event, context)
		states[event.id], effects[event.id] = state, effect
		if recorded is not None and recorded.get(event.id) == state:
			skipped = tuple(later.id for later in ordered[index + 1 :])
			return ReplayResult(states, effects, state, event.id, skipped)
	return ReplayResult(states, effects, state)


def replay_after_insert(
	events: Iterable[Event],
	inserted: Event,
	prior: ReplayResult,
	context: EngineContext,
) -> ReplayResult:
	"""Recompute from the nearest prior state before `inserted`, converging early
	against `prior`. `events` must already include `inserted`. The result covers
	only the recomputed tail; earlier events keep their `prior` states."""
	ordered = sort_events(events)
	before = [event for event in ordered if event.sort_key < inserted.sort_key]
	if not before:
		return replay(ordered, context, recorded=prior.states)
	snapshot = before[-1]
	tail = [event for event in ordered if event.sort_key > snapshot.sort_key]
	return replay(tail, context, start=prior.states[snapshot.id], recorded=prior.states)


def sort_events(events: Iterable[Event]) -> list[Event]:
	ordered = sorted(events, key=lambda event: event.sort_key)
	ids = [event.id for event in ordered]
	if len(set(ids)) != len(ids):
		raise ValueError("duplicate event ids")
	return ordered
