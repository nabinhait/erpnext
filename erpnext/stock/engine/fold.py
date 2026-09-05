"""The pure fold: (State, Event) -> (State, Effect). Total, deterministic, no I/O.

The fold never raises for business reasons — a negative balance becomes a
modelled exposure with a provisional rate, reported on the Effect. The caller
decides whether that is an error. Allocated (lot-tracked) movements run the
same transitions within each lot's sub-state; specific identification for
serials falls out of that with no dedicated code.
"""
from __future__ import annotations

from bisect import bisect_left
from operator import attrgetter

from .context import FoldContext
from .event import Event, EventKind
from .lots import Allocation, LotType
from .state import Effect, Layer, LotState, State, validate_lot, validate_state

_EMPTY_STATE = State()
_SORT_KEY = attrgetter("sort_key")


def fold(state: State, event: Event, context: FoldContext) -> tuple[State, Effect]:
	handler = _HANDLERS[event.kind]
	new_state, effect = handler(state, event, context)
	validate_state(new_state, deep=False)
	return new_state, effect


def _receive(state: State, event: Event, context: FoldContext) -> tuple[State, Effect]:
	if event.allocations:
		return _fold_allocations(state, event, context)
	new_state, true_up, variance = _add_stock(
		state, event.qty_change, event.declared_rate, event.id, context)
	return new_state, _effect(state, new_state, event, true_up=true_up, variance=variance)


def _revalue(state: State, event: Event, context: FoldContext) -> tuple[State, Effect]:
	"""A cost revision (landed cost) on a prior receipt's surviving layers.

	Applies a uniform per-unit uplift to the layers the source event created —
	top-level and inside lots. Ordered at the source's instant (with a later
	id), so everything downstream trues up in the same refold. If nothing of
	the source survives at this point, the revision is a no-op here and the
	caller carries the residue."""
	matched_qty = _matched_qty(state, event.reverses_event)
	if matched_qty <= 0:
		return state, _effect(state, state, event)

	per_unit = event.value_change / matched_qty
	layers = tuple(_uplifted(layer, event.reverses_event, per_unit) for layer in state.layers)
	lots = tuple(
		LotState(
			lot_type=lot.lot_type,
			lot_id=lot.lot_id,
			state=State(
				layers=tuple(_uplifted(layer, event.reverses_event, per_unit) for layer in lot.state.layers),
				exposure_qty=lot.state.exposure_qty,
				exposure_rate=lot.state.exposure_rate,
			),
		)
		for lot in state.lots
	)
	new_state = State(
		layers=layers, exposure_qty=state.exposure_qty, exposure_rate=state.exposure_rate, lots=lots)
	return new_state, _effect(state, new_state, event)


def _matched_qty(state: State, source_event_id: int) -> float:
	qty = sum(layer.qty for layer in state.layers if layer.source_event_id == source_event_id)
	for lot in state.lots:
		qty += sum(layer.qty for layer in lot.state.layers if layer.source_event_id == source_event_id)
	return qty


def _uplifted(layer: Layer, source_event_id: int, per_unit: float) -> Layer:
	if layer.source_event_id != source_event_id:
		return layer
	return Layer(layer.qty, layer.rate + per_unit, layer.source_event_id)


def _issue(state: State, event: Event, context: FoldContext) -> tuple[State, Effect]:
	if event.allocations:
		return _fold_allocations(state, event, context)
	qty = -event.qty_change
	new_state, cost, negative = _remove_stock(
		state, qty, context, prefer_rate=event.declared_rate,
		rate_buckets=event.rate_buckets)
	effect = _effect(state, new_state, event, consumed_rate=cost / qty, negative=negative)
	return new_state, effect


def _assert_balance(state: State, event: Event, context: FoldContext) -> tuple[State, Effect]:
	"""A reconciliation asserts what the whole balance IS, independent of prior history.

	assert_rate prices the unlotted pool; allocations seed lot sub-states
	(one layer each, at the allocation's declared_rate) — a lot absent from
	the allocations is asserted gone. A negative assert_qty freezes a legacy
	uncovered balance as modelled exposure, settled by future receipts.
	"""
	if event.assert_qty < 0:
		new_state = State(exposure_qty=-event.assert_qty, exposure_rate=event.assert_rate)
		return new_state, _effect(state, new_state, event)
	lots = []
	allocated = 0.0
	for allocation in event.allocations:
		rate = allocation.declared_rate
		rate = rate if rate is not None else event.assert_rate
		lot = LotState(
			allocation.lot_type, allocation.lot_id,
			State(layers=(Layer(allocation.qty, rate, event.id),)),
		)
		validate_lot(lot)
		lots.append(lot)
		allocated += allocation.qty
	layers: tuple[Layer, ...] = ()
	pool = event.assert_qty - allocated
	if pool > 1e-9:
		layers = (Layer(pool, event.assert_rate, event.id),)
	new_state = State(layers=layers, lots=tuple(sorted(lots, key=_SORT_KEY)))
	return new_state, _effect(state, new_state, event)


def _reverse(state: State, event: Event, context: FoldContext) -> tuple[State, Effect]:
	"""A reversal is an inverse movement. Reversing a receipt takes back its own
	surviving layer first; anything already consumed leaves via the normal policy."""
	if event.allocations:
		return _fold_allocations(state, event, context)
	if event.qty_change > 0:
		new_state, true_up, variance = _add_stock(
			state, event.qty_change, event.declared_rate, event.id, context)
		return new_state, _effect(state, new_state, event, true_up=true_up, variance=variance)
	qty = -event.qty_change
	new_state, cost, negative = _remove_stock(
		state, qty, context, prefer_source=event.reverses_event)
	effect = _effect(state, new_state, event, consumed_rate=cost / qty, negative=negative)
	return new_state, effect


def _fold_allocations(state: State, event: Event, context: FoldContext) -> tuple[State, Effect]:
	"""Apply each allocation to its lot's sub-state; the key aggregates the lots.

	Any qty_change beyond the allocations is unlotted stock (batches that are
	quantity tags, not sub-folds) and folds against the key's shared top-level
	pool — the pools never borrow from each other.

	Validates only the touched lots' final sub-states — untouched lots are
	immutable and were validated when they last changed."""
	prefer = event.reverses_event if event.kind is EventKind.REVERSAL else None
	current = state
	cost = outward = true_up = variance = 0.0
	negative = False
	touched: dict[tuple[LotType, str], State] = {}
	for allocation in event.allocations:
		index, present = _locate_lot(current.lots, allocation.lot_type, allocation.lot_id)
		sub = current.lots[index].state if present else _EMPTY_STATE
		if allocation.qty > 0:
			rate = allocation.declared_rate
			rate = rate if rate is not None else event.declared_rate
			sub, sub_true_up, sub_variance = _add_stock(
				sub, allocation.qty, rate, event.id, context)
			true_up += sub_true_up
			variance += sub_variance
		else:
			sub, sub_cost, sub_negative = _remove_stock(
				sub, -allocation.qty, context, prefer_source=prefer,
				prefer_rate=event.declared_rate)
			cost += sub_cost
			outward += -allocation.qty
			negative = negative or sub_negative
		touched[(allocation.lot_type, allocation.lot_id)] = sub
		current = _set_lot(current, allocation, sub, index, present)
	for (lot_type, lot_id), sub in touched.items():
		validate_lot(LotState(lot_type, lot_id, sub))
	remainder = event.qty_change - sum(a.qty for a in event.allocations)
	if remainder > 1e-9:
		current, pool_true_up, pool_variance = _add_stock(
			current, remainder, event.declared_rate, event.id, context)
		true_up += pool_true_up
		variance += pool_variance
	elif remainder < -1e-9:
		current, pool_cost, pool_negative = _remove_stock(
			current, -remainder, context, prefer_source=prefer,
			prefer_rate=event.declared_rate)
		cost += pool_cost
		outward += -remainder
		negative = negative or pool_negative
	effect = _effect(
		state, current, event,
		consumed_rate=cost / outward if outward else None,
		negative=negative, true_up=true_up, variance=variance,
	)
	return current, effect


def _add_stock(
	state: State, qty: float, rate: float, event_id: int, context: FoldContext
) -> tuple[State, float, float]:
	"""Returns (new_state, true_up, variance). Covers exposure before layering."""
	cover = min(qty, state.exposure_qty)
	true_up = cover * (rate - state.exposure_rate)
	into_layers = qty - cover
	layers, variance = state.layers, 0.0
	if into_layers > 0:
		layers, variance = context.policy.receive(state.layers, into_layers, rate, event_id)
	exposure_left = state.exposure_qty - cover
	new_state = State(
		layers=layers,
		exposure_qty=exposure_left,
		exposure_rate=state.exposure_rate if exposure_left > 0 else 0.0,
		lots=state.lots,
	)
	return new_state, true_up, variance


def _remove_stock(
	state: State, qty: float, context: FoldContext, prefer_source: int | None = None,
	prefer_rate: float | None = None,
	rate_buckets: tuple[tuple[float, float], ...] = (),
) -> tuple[State, float, bool]:
	"""Returns (new_state, cost, negative). Shortfall becomes provisional exposure.

	rate_buckets are serial-wise picks: each (qty, rate) group consumes its
	matching layers first, so every picked unit leaves at its own receipt
	rate. prefer_rate models a cost-linked leg: an issue that a business
	document declared to move specific stock (a transfer's inward leg
	consuming from transit, a purchase return at the original receipt's
	rate). Anything left falls back to the policy."""
	layers, cost, taken = state.layers, 0.0, 0.0
	if prefer_source is not None:
		layers, cost, taken = _take_from_source(layers, qty, prefer_source)
	for bucket_qty, bucket_rate in rate_buckets:
		if taken >= qty:
			break
		layers, bucket_cost, bucket_taken = _take_at_rate(
			layers, min(bucket_qty, qty - taken), bucket_rate)
		cost += bucket_cost
		taken += bucket_taken
	if prefer_rate is not None and taken < qty:
		layers, rate_cost, rate_taken = _take_at_rate(layers, qty - taken, prefer_rate)
		cost += rate_cost
		taken += rate_taken
	held = sum(layer.qty for layer in layers)
	from_policy = min(qty - taken, held)
	if from_policy > 0:
		layers, policy_cost = context.policy.consume(layers, from_policy)
		cost += policy_cost
	shortfall = qty - taken - from_policy
	exposure_qty, exposure_rate = state.exposure_qty, state.exposure_rate
	if shortfall > 0:
		provisional = _provisional_rate(cost, taken + from_policy, state, context)
		cost += shortfall * provisional
		exposure_qty, exposure_rate = _blend(exposure_qty, exposure_rate, shortfall, provisional)
	new_state = State(
		layers=layers, exposure_qty=exposure_qty, exposure_rate=exposure_rate, lots=state.lots)
	return new_state, cost, shortfall > 0


def _take_from_source(
	layers: tuple[Layer, ...], qty: float, source_event_id: int
) -> tuple[tuple[Layer, ...], float, float]:
	"""Take up to qty from the layer(s) a specific event created, at their own rate."""
	kept: list[Layer] = []
	cost, taken = 0.0, 0.0
	for layer in layers:
		if layer.source_event_id != source_event_id or taken >= qty:
			kept.append(layer)
			continue
		take = min(qty - taken, layer.qty)
		cost += take * layer.rate
		taken += take
		if layer.qty > take:
			kept.append(Layer(layer.qty - take, layer.rate, layer.source_event_id))
	return tuple(kept), cost, taken


def _take_at_rate(
	layers: tuple[Layer, ...], qty: float, rate: float
) -> tuple[tuple[Layer, ...], float, float]:
	"""Take up to qty from the layers carrying exactly this rate (oldest first)."""
	kept: list[Layer] = []
	cost, taken = 0.0, 0.0
	for layer in layers:
		if abs(layer.rate - rate) > 1e-9 or taken >= qty:
			kept.append(layer)
			continue
		take = min(qty - taken, layer.qty)
		cost += take * layer.rate
		taken += take
		if layer.qty > take:
			kept.append(Layer(layer.qty - take, layer.rate, layer.source_event_id))
	return tuple(kept), cost, taken


def _set_lot(
	state: State, allocation: Allocation, sub: State, index: int, present: bool
) -> State:
	"""Replace one lot's sub-state, keeping lots sorted and dropping emptied lots."""
	return State(
		layers=state.layers,
		exposure_qty=state.exposure_qty,
		exposure_rate=state.exposure_rate,
		lots=_splice_lot(state.lots, allocation, sub, index, present),
	)


def _splice_lot(
	lots: tuple[LotState, ...], allocation: Allocation, sub: State, index: int, present: bool
) -> tuple[LotState, ...]:
	"""Splice one entry into the sorted lots tuple at its pre-located index — no re-sort."""
	if sub == _EMPTY_STATE:
		return lots[:index] + lots[index + 1:] if present else lots
	entry = LotState(allocation.lot_type, allocation.lot_id, sub)
	return lots[:index] + (entry,) + lots[index + 1 if present else index:]


def _locate_lot(
	lots: tuple[LotState, ...], lot_type: LotType, lot_id: str
) -> tuple[int, bool]:
	"""Bisect the sorted lots tuple: (position of the lot or its insertion point, found?)."""
	key = (lot_type.value, lot_id)
	index = bisect_left(lots, key, key=_SORT_KEY)
	return index, index < len(lots) and lots[index].sort_key == key


def _provisional_rate(cost: float, covered_qty: float, state: State, context: FoldContext) -> float:
	if covered_qty > 0:
		return cost / covered_qty
	if state.exposure_qty > 0:
		return state.exposure_rate
	return context.fallback_rate


def _blend(qty_a: float, rate_a: float, qty_b: float, rate_b: float) -> tuple[float, float]:
	total = qty_a + qty_b
	return total, (qty_a * rate_a + qty_b * rate_b) / total


def _effect(
	old: State, new: State, event: Event, *,
	consumed_rate: float | None = None,
	negative: bool = False,
	true_up: float = 0.0,
	variance: float = 0.0,
) -> Effect:
	return Effect(
		event_id=event.id,
		qty_after=new.qty,
		value_after=new.value,
		value_delta=new.value - old.value,
		consumed_rate=consumed_rate,
		negative=negative,
		true_up=true_up,
		variance=variance,
	)


_HANDLERS = {
	EventKind.OPENING: _receive,
	EventKind.RECEIPT: _receive,
	EventKind.ISSUE: _issue,
	EventKind.ASSERTION: _assert_balance,
	EventKind.REVERSAL: _reverse,
	EventKind.REVALUATION: _revalue,
}
