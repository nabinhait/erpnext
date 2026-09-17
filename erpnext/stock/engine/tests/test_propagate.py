"""Cross-key propagation: the walker in core/propagate.py."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from erpnext.stock.engine import (
	CostPool,
	EngineContext,
	Event,
	EventKind,
	Fifo,
	Leg,
	PooledLeg,
	PropagationResult,
	Voucher,
	apply_voucher,
	propagate_cost_pools,
	replay,
)

FIFO = EngineContext(policy=Fifo())
BASE = datetime(2025, 1, 1)


@st.composite
def transfer_graphs(draw, allow_diamond: bool = False, allow_back_edge: bool = False):
	"""Consistent multi-key transfer scenarios with a backdated trigger on K0."""
	context = EngineContext(policy=Fifo(), fallback_rate=7)
	graph = _Graph(context)
	_random_ops(draw, graph, "K0", draw(st.integers(1, 4)))
	diamond = allow_diamond and draw(st.booleans())
	hops = draw(st.integers(2, 4))
	for hop in range(1, hops):
		source, target = f"K{hop - 1}", f"K{hop}"
		for _ in range(2 if hop == 1 and diamond else 1):
			graph.receipt(source, draw(st.integers(20, 100)), draw(st.integers(1, 50)))
			graph.transfer(source, target, draw(st.integers(1, 20)), draw(st.integers(0, 30)))
		_random_ops(draw, graph, target, draw(st.integers(0, 2)))
	if allow_back_edge and draw(st.booleans()):
		_back_edge(draw, graph, hops)
	minute = draw(st.integers(0, graph.minute))
	inserted = Event(
		9999,
		BASE + timedelta(minutes=minute, seconds=30),
		EventKind.RECEIPT,
		draw(st.integers(1, 100)),
		draw(st.integers(1, 50)),
	)
	graph.emit("K0", inserted)
	return graph.streams, graph.pools, inserted, context


def transfer_scenario(
	target_qty: float = 50,
	extra_cost: float = 0.0,
) -> tuple[dict[str, list[Event]], list[CostPool]]:
	"""The scratchpad demo posted state: Stores receipts, one transfer into WIP."""
	stores = [
		Event(1, day(1, 1), EventKind.RECEIPT, 30, 10),
		Event(2, day(1, 20), EventKind.RECEIPT, 70, 12),
	]
	outgoing = Leg("Stores", Event(10, day(2, 1), EventKind.ISSUE, -50))
	pool = CostPool((10,), (PooledLeg("WIP", 11, day(2, 1), target_qty),), extra_cost)
	inward = realize(Voucher((outgoing,), (pool,)), {"Stores": replay(stores, FIFO).final}, "WIP")
	return {"Stores": [*stores, outgoing.event], "WIP": [inward]}, [pool]


def round_trip_scenario() -> tuple[dict[str, list[Event]], list[CostPool], Event]:
	"""Mumbai -> Pune -> Mumbai, then a Mumbai delivery, then a backdated receipt.

	The key graph loops back to the trigger key; the event graph still runs
	forward, so the walker recomputes Mumbai a second time from Feb 12.
	"""
	mumbai = [Event(1, day(2, 1), EventKind.RECEIPT, 10, 100)]
	outward, pune_pool = transfer_voucher("Mumbai", "Pune", day(2, 10), 5, 10)
	pune_inward = realize(outward, {"Mumbai": replay(mumbai, FIFO).final}, "Pune")
	homeward, mumbai_pool = transfer_voucher("Pune", "Mumbai", day(2, 12), 3, 20)
	mumbai_inward = realize(homeward, {"Pune": replay([pune_inward], FIFO).final}, "Mumbai")
	streams = {
		"Mumbai": [*mumbai, outward.legs[0].event, mumbai_inward, Event(30, day(2, 20), EventKind.ISSUE, -4)],
		"Pune": [pune_inward, homeward.legs[0].event],
	}
	backdated = Event(40, day(1, 15), EventKind.RECEIPT, 20, 150)
	streams["Mumbai"].append(backdated)
	return streams, [pune_pool, mumbai_pool], backdated


def manufacture_scenario() -> tuple[dict[str, list[Event]], list[CostPool], Event]:
	"""RM1 5 qty from Mumbai + RM2 8 qty + labour 200 -> FG1 4 qty and scrap 2 qty.

	Then the Jan 15 receipt backdates RM1's cost from 100 to 150.
	"""
	mumbai = [Event(1, day(2, 1), EventKind.RECEIPT, 10, 100)]
	rm2 = [Event(2, day(2, 1), EventKind.RECEIPT, 20, 40)]
	voucher = Voucher(
		(
			Leg("Mumbai", Event(10, day(2, 10), EventKind.ISSUE, -5)),
			Leg("RM2", Event(11, day(2, 10), EventKind.ISSUE, -8)),
		),
		(
			CostPool(
				(10, 11),
				(
					PooledLeg("FG1", 12, day(2, 10), 4),
					PooledLeg("Scrap", 13, day(2, 10), 2, fixed_rate=25),
				),
				extra_cost=200,
			),
		),
	)
	states = {"Mumbai": replay(mumbai, FIFO).final, "RM2": replay(rm2, FIFO).final}
	posted = apply_voucher(states, voucher, FIFO)
	streams = {
		"Mumbai": [*mumbai, voucher.legs[0].event],
		"RM2": [*rm2, voucher.legs[1].event],
		"FG1": [realized_inward(posted, "FG1"), Event(20, day(2, 20), EventKind.ISSUE, -1)],
		"Scrap": [realized_inward(posted, "Scrap")],
	}
	backdated = Event(40, day(1, 15), EventKind.RECEIPT, 20, 150)
	streams["Mumbai"].append(backdated)
	return streams, list(voucher.pools), backdated


def transfer_voucher(
	source: str, target: str, when: datetime, qty: float, outgoing_id: int
) -> tuple[Voucher, CostPool]:
	"""One transfer voucher: the outgoing leg values itself, the pool feeds the inward leg."""
	pool = CostPool((outgoing_id,), (PooledLeg(target, outgoing_id + 1, when, qty),))
	return Voucher((Leg(source, Event(outgoing_id, when, EventKind.ISSUE, -qty)),), (pool,)), pool


def realize(voucher: Voucher, states: dict, key: str) -> Event:
	"""The inward Event a voucher posts to `key`, valued from `states`."""
	return realized_inward(apply_voucher(states, voucher, FIFO), key)


def realized_inward(posted, key: str) -> Event:
	return next(leg.event for leg in posted.realized_legs if leg.key == key)


def day(month: int, day_of_month: int) -> datetime:
	return datetime(2025, month, day_of_month)


def close(a: float, b: float) -> bool:
	return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def _back_edge(draw, graph: _Graph, hops: int) -> None:
	"""Return stock from a downstream key to an upstream one.

	The key graph then holds a cycle while the event graph does not — the
	returning leg is always stamped later than the leg that fed it.
	"""
	source_hop = draw(st.integers(1, hops - 1))
	source, target = f"K{source_hop}", f"K{draw(st.integers(0, source_hop - 1))}"
	if graph.held(source) < 1:
		graph.receipt(source, draw(st.integers(20, 100)), draw(st.integers(1, 50)))
	graph.transfer(source, target, draw(st.integers(1, int(graph.held(source)))), draw(st.integers(0, 30)))
	_random_ops(draw, graph, target, draw(st.integers(0, 2)))


def _random_ops(draw, graph: _Graph, key: str, count: int) -> None:
	for _ in range(count):
		held = graph.held(key)
		if held >= 1 and draw(st.booleans()):
			graph.issue(key, draw(st.integers(1, int(held))))
		else:
			graph.receipt(key, draw(st.integers(1, 100)), draw(st.integers(1, 50)))


class _Graph:
	"""Builds time-ordered multi-key streams whose pools start out consistent."""

	def __init__(self, context: EngineContext) -> None:
		self.context = context
		self.streams: dict[str, list[Event]] = {"K0": []}
		self.pools: list[CostPool] = []
		self.next_id = 1
		self.minute = 0

	def receipt(self, key: str, qty: float, rate: float) -> None:
		event_id, when = self.stamp()
		self.emit(key, Event(event_id, when, EventKind.RECEIPT, qty, rate))

	def issue(self, key: str, qty: float) -> Event:
		event_id, when = self.stamp()
		event = Event(event_id, when, EventKind.ISSUE, -qty)
		self.emit(key, event)
		return event

	def transfer(self, source: str, target: str, qty: float, extra_cost: float) -> None:
		outgoing = self.issue(source, qty)
		cost = -replay(self.streams[source], self.context).effects[outgoing.id].value_delta
		event_id, when = self.stamp()
		self.emit(target, Event(event_id, when, EventKind.RECEIPT, qty, (cost + extra_cost) / qty))
		self.pools.append(CostPool((outgoing.id,), (PooledLeg(target, event_id, when, qty),), extra_cost))

	def held(self, key: str) -> float:
		return replay(self.streams.get(key, []), self.context).final.qty

	def emit(self, key: str, event: Event) -> None:
		self.streams.setdefault(key, []).append(event)

	def stamp(self) -> tuple[int, datetime]:
		event_id, when = self.next_id, BASE + timedelta(minutes=self.minute)
		self.next_id += 1
		self.minute += 10
		return event_id, when


class TestPropagateFunctions(unittest.TestCase):
	def test_one_hop_backdate_re_realizes_transfer(self) -> None:
		"""The scratchpad demo: Stores backdate moves the transfer rate 10.8 -> 9.2."""
		streams, pools = transfer_scenario()
		streams["WIP"].append(Event(12, day(3, 1), EventKind.ISSUE, -20))
		assert close(streams["WIP"][0].declared_rate, 10.8)
		wip_before = replay(streams["WIP"], FIFO)
		backdated = Event(20, day(1, 15), EventKind.RECEIPT, 40, 8)
		streams["Stores"].append(backdated)

		result = propagate_cost_pools(streams, pools, ("Stores", backdated), FIFO)

		assert close(-result.recomputes["Stores"].effects[10].value_delta, 9.2 * 50)
		(re_realized,) = result.re_realized_events
		assert re_realized.id == 11 and close(re_realized.declared_rate, 9.2)
		wip = result.recomputes["WIP"]
		assert close(wip.effects[11].value_delta - wip_before.effects[11].value_delta, -80)
		assert close(wip.effects[12].value_delta - wip_before.effects[12].value_delta, 32)
		assert result.invalidations == (("Stores", 20), ("WIP", 11))
		assert close(wip.final.qty, 30) and close(wip.final.value, 276)

	def test_backdated_raw_material_reprices_the_finished_good_only(self) -> None:
		"""Doc §1.9's untested case: RM1 100 -> 150 moves the pool 1020 -> 1270.
		Scrap stays at its own rate; the whole 250 lands on FG1."""
		streams, pools, backdated = manufacture_scenario()
		assert close(streams["FG1"][0].declared_rate, 242.5)

		result = propagate_cost_pools(streams, pools, ("Mumbai", backdated), FIFO)

		assert close(result.streams["FG1"][0].declared_rate, 305)
		assert close(result.streams["Scrap"][0].declared_rate, 25)
		assert close(result.recomputes["Scrap"].final.value, 50)
		assert close(result.recomputes["FG1"].final.value, 3 * 305)
		assert {event.id for event in result.re_realized_events} == {12, 13}
		assert result.invalidations == (("Mumbai", 40), ("FG1", 12), ("Scrap", 13))
		for key, recompute in result.recomputes.items():
			assert recompute.final == replay(result.streams[key], FIFO).final

	def test_finished_good_cogs_follows_the_repriced_pool(self) -> None:
		"""The FG issue after the manufacture costs what the new pool made it worth."""
		streams, pools, backdated = manufacture_scenario()
		before = replay(streams["FG1"], FIFO)

		result = propagate_cost_pools(streams, pools, ("Mumbai", backdated), FIFO)

		assert close(before.effects[20].value_delta, -242.5)
		assert close(result.recomputes["FG1"].effects[20].value_delta, -305)

	def test_unchanged_raw_material_does_not_fire_its_pool(self) -> None:
		"""Cut 2 with several sources: RM2 alone moving nothing leaves the pool alone."""
		streams, pools, _ = manufacture_scenario()
		late = Event(41, day(3, 1), EventKind.RECEIPT, 5, 99)
		streams["RM2"].append(late)

		result = propagate_cost_pools(streams, pools, ("RM2", late), FIFO)

		assert result.re_realized_events == ()
		assert result.invalidations == (("RM2", 41),)

	def test_round_trip_transfer_recomputes_the_trigger_key_twice(self) -> None:
		"""Mumbai -> Pune -> Mumbai: the second hop re-enters the key that triggered
		the walk, so Mumbai recomputes again from its own overlaid states."""
		streams, pools, backdated = round_trip_scenario()
		assert close(streams["Mumbai"][2].declared_rate, 100)

		result = propagate_cost_pools(streams, pools, ("Mumbai", backdated), FIFO)

		assert result.invalidations == (("Mumbai", 40), ("Pune", 11), ("Mumbai", 21))
		assert [event.id for event in result.re_realized_events] == [11, 21]
		# both Mumbai passes survive the merge: the caller persists one complete suffix
		assert set(result.recomputes["Mumbai"].states) == {40, 1, 10, 21, 30}
		for key, recompute in result.recomputes.items():
			full = replay(result.streams[key], FIFO)
			assert recompute.final == full.final
			for event_id, state in recompute.states.items():
				assert full.states[event_id] == state

	def test_round_trip_returned_stock_carries_the_new_cost(self) -> None:
		"""The returned units re-enter at the backdated rate, as a layer dated Feb 12 —
		so FIFO leaves them behind the Jan 15 layer the delivery consumes."""
		streams, pools, backdated = round_trip_scenario()

		result = propagate_cost_pools(streams, pools, ("Mumbai", backdated), FIFO)

		mumbai, pune = result.recomputes["Mumbai"], result.recomputes["Pune"]
		assert [(layer.qty, layer.rate) for layer in mumbai.final.layers] == [(11, 150), (10, 100), (3, 150)]
		assert close(mumbai.final.qty, 24) and close(mumbai.final.value, 3100)
		assert close(pune.final.qty, 2) and close(pune.final.value, 300)
		assert close(pune.effects[20].value_delta, -450)
		assert close(mumbai.effects[30].value_delta, -600)

	def test_reconciliation_between_backdate_and_transfer_cuts_propagation(self) -> None:
		"""Cut 1: the trigger recompute converges at the assertion; the pool never fires."""
		a = [
			Event(1, day(1, 1), EventKind.RECEIPT, 30, 10),
			Event(2, day(1, 20), EventKind.RECEIPT, 70, 12),
			Event(5, day(1, 25), EventKind.ASSERTION, assert_qty=100, assert_rate=11),
			Event(10, day(2, 1), EventKind.ISSUE, -50),
		]
		b_in = Event(11, day(2, 1), EventKind.RECEIPT, 50, 11)
		backdated = Event(30, day(1, 15), EventKind.RECEIPT, 40, 8)
		streams = {"A": [*a, backdated], "B": [b_in]}
		pools = [CostPool((10,), (PooledLeg("B", 11, day(2, 1), 50),))]

		result = propagate_cost_pools(streams, pools, ("A", backdated), FIFO)

		assert result.invalidations == (("A", 30),)
		assert result.re_realized_events == ()
		assert "B" not in result.recomputes
		assert result.streams["B"] == [b_in]
		assert result.recomputes["A"].converged_at == 5
		assert 10 in result.recomputes["A"].skipped

	def test_backdate_landing_after_consumed_range_does_not_fire(self) -> None:
		"""Cut 2: the recompute reaches the transfer but its consumed cost is unchanged."""
		a = [
			Event(1, day(1, 1), EventKind.RECEIPT, 60, 10),
			Event(10, day(2, 1), EventKind.ISSUE, -50),
		]
		b_in = Event(11, day(2, 1), EventKind.RECEIPT, 50, 10)
		backdated = Event(30, day(1, 15), EventKind.RECEIPT, 40, 12)
		streams = {"A": [*a, backdated], "B": [b_in]}
		pools = [CostPool((10,), (PooledLeg("B", 11, day(2, 1), 50),))]

		result = propagate_cost_pools(streams, pools, ("A", backdated), FIFO)

		assert 10 in result.recomputes["A"].effects
		assert close(result.recomputes["A"].effects[10].consumed_rate, 10)
		assert result.re_realized_events == ()
		assert result.invalidations == (("A", 30),)
		assert result.streams["B"] == [b_in]

	def test_extra_cost_stays_absolute_on_re_realization(self) -> None:
		"""A repack: the pool moves with the raw material, the 100 operating cost does not."""
		streams, pools = transfer_scenario(target_qty=25, extra_cost=100)
		assert close(streams["WIP"][0].declared_rate, (10.8 * 50 + 100) / 25)
		backdated = Event(20, day(1, 15), EventKind.RECEIPT, 40, 8)
		streams["Stores"].append(backdated)

		result = propagate_cost_pools(streams, pools, ("Stores", backdated), FIFO)

		expected_rate = (9.2 * 50 + 100) / 25
		assert close(result.streams["WIP"][0].declared_rate, expected_rate)
		assert close(result.recomputes["WIP"].final.value, 25 * expected_rate)

	@given(transfer_graphs(allow_back_edge=True))
	def test_cascade_equivalence_after_propagate(self, scenario) -> None:
		"""Load-bearing: every recomputed key equals a from-scratch replay of its stream."""
		streams, pools, inserted, context = scenario
		result = propagate_cost_pools(streams, pools, ("K0", inserted), context)
		for key, stream in result.streams.items():
			recompute = result.recomputes.get(key)
			if recompute is None:
				continue
			full = replay(stream, context)
			assert recompute.final == full.final
			for event_id, state in recompute.states.items():
				assert full.states[event_id] == state

	@given(transfer_graphs(allow_diamond=True, allow_back_edge=True))
	def test_propagation_terminates_on_random_graphs(self, scenario) -> None:
		streams, pools, inserted, context = scenario
		result = propagate_cost_pools(streams, pools, ("K0", inserted), context)
		assert isinstance(result, PropagationResult)
		assert len(result.invalidations) <= 1 + len(pools) * (len(pools) + 1)
