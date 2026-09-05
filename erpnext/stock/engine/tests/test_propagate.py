"""Cross-key propagation: the walker in core/propagate.py."""
from __future__ import annotations

import unittest

import math
from datetime import datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from erpnext.stock.engine import (
	CostLink,
	CostLinkedLeg,
	Event,
	EventKind,
	Fifo,
	FoldContext,
	Leg,
	PropagationResult,
	Voucher,
	fold_voucher,
	propagate,
	replay,
)

FIFO = FoldContext(policy=Fifo())
BASE = datetime(2025, 1, 1)  # noqa: DTZ001 — business posting time is naive by design


@st.composite
def transfer_graphs(draw, allow_diamond: bool = False):
	"""Consistent multi-key transfer scenarios with a backdated trigger on K0."""
	context = FoldContext(policy=Fifo(), fallback_rate=7)
	graph = _Graph(context)
	_random_ops(draw, graph, "K0", draw(st.integers(1, 4)))
	diamond = allow_diamond and draw(st.booleans())
	for hop in range(1, draw(st.integers(2, 4))):
		source, target = f"K{hop - 1}", f"K{hop}"
		for _ in range(2 if hop == 1 and diamond else 1):
			graph.receipt(source, draw(st.integers(20, 100)), draw(st.integers(1, 50)))
			graph.transfer(source, target, draw(st.integers(1, 20)), draw(st.integers(0, 30)))
		_random_ops(draw, graph, target, draw(st.integers(0, 2)))
	minute = draw(st.integers(0, graph.minute))
	inserted = Event(
		9999, BASE + timedelta(minutes=minute, seconds=30),
		EventKind.RECEIPT, draw(st.integers(1, 100)), draw(st.integers(1, 50)))
	graph.emit("K0", inserted)
	return graph.streams, graph.links, inserted, context


def transfer_scenario(
	target_qty: float = 50, cost_share_qty: float | None = None, extra_cost: float = 0.0,
) -> tuple[dict[str, list[Event]], list[CostLink]]:
	"""The scratchpad demo posted state: Stores receipts, one transfer into WIP."""
	stores = [
		Event(1, day(1, 1), EventKind.RECEIPT, 30, 10),
		Event(2, day(1, 20), EventKind.RECEIPT, 70, 12),
	]
	transfer = Voucher(legs=(
		Leg("Stores", Event(10, day(2, 1), EventKind.ISSUE, -50)),
		CostLinkedLeg(
			key="WIP", id=11, posting_datetime=day(2, 1), qty_change=target_qty,
			cost_from=10, extra_cost=extra_cost, cost_share_qty=cost_share_qty),
	))
	posted = fold_voucher({"Stores": replay(stores, FIFO).final}, transfer, FIFO)
	inward = realized_inward(posted, "WIP")
	streams = {"Stores": [*stores, transfer.legs[0].event], "WIP": [inward]}
	share = cost_share_qty if cost_share_qty is not None else target_qty
	return streams, [CostLink(10, "WIP", 11, share, extra_cost)]


def realized_inward(posted, key: str) -> Event:
	return next(leg.event for leg in posted.realized_legs if leg.key == key)


def day(month: int, day_of_month: int) -> datetime:
	return datetime(2025, month, day_of_month)  # noqa: DTZ001


def close(a: float, b: float) -> bool:
	return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def _random_ops(draw, graph: _Graph, key: str, count: int) -> None:
	for _ in range(count):
		held = graph.held(key)
		if held >= 1 and draw(st.booleans()):
			graph.issue(key, draw(st.integers(1, int(held))))
		else:
			graph.receipt(key, draw(st.integers(1, 100)), draw(st.integers(1, 50)))


class _Graph:
	"""Builds time-ordered multi-key streams whose links start out consistent."""

	def __init__(self, context: FoldContext) -> None:
		self.context = context
		self.streams: dict[str, list[Event]] = {"K0": []}
		self.links: list[CostLink] = []
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
		rate = replay(self.streams[source], self.context).effects[outgoing.id].consumed_rate
		event_id, when = self.stamp()
		declared = (rate * qty + extra_cost) / qty
		self.emit(target, Event(event_id, when, EventKind.RECEIPT, qty, declared))
		self.links.append(CostLink(outgoing.id, target, event_id, qty, extra_cost))

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
		streams, links = transfer_scenario()
		streams["WIP"].append(Event(12, day(3, 1), EventKind.ISSUE, -20))
		assert close(streams["WIP"][0].declared_rate, 10.8)
		wip_before = replay(streams["WIP"], FIFO)
		backdated = Event(20, day(1, 15), EventKind.RECEIPT, 40, 8)
		streams["Stores"].append(backdated)

		result = propagate(streams, links, ("Stores", backdated), FIFO)

		assert close(result.refolds["Stores"].effects[10].consumed_rate, 9.2)
		(re_realized,) = result.re_realized_events
		assert re_realized.id == 11 and close(re_realized.declared_rate, 9.2)
		wip = result.refolds["WIP"]
		assert close(wip.effects[11].value_delta - wip_before.effects[11].value_delta, -80)
		assert close(wip.effects[12].value_delta - wip_before.effects[12].value_delta, 32)
		assert result.invalidations == (("Stores", 20), ("WIP", 11))
		assert close(wip.final.qty, 30) and close(wip.final.value, 276)

	def test_two_hop_chain_propagates_through_middle_key(self) -> None:
		a = [
			Event(1, day(1, 1), EventKind.RECEIPT, 30, 10),
			Event(2, day(1, 20), EventKind.RECEIPT, 70, 12),
		]
		first = Voucher(legs=(
			Leg("A", Event(10, day(2, 1), EventKind.ISSUE, -50)),
			CostLinkedLeg(key="B", id=11, posting_datetime=day(2, 1), qty_change=50, cost_from=10),
		))
		b_in = realized_inward(fold_voucher({"A": replay(a, FIFO).final}, first, FIFO), "B")
		second = Voucher(legs=(
			Leg("B", Event(20, day(3, 1), EventKind.ISSUE, -50)),
			CostLinkedLeg(key="C", id=21, posting_datetime=day(3, 1), qty_change=50, cost_from=20),
		))
		c_in = realized_inward(fold_voucher({"B": replay([b_in], FIFO).final}, second, FIFO), "C")
		streams = {
			"A": [*a, first.legs[0].event],
			"B": [b_in, second.legs[0].event],
			"C": [c_in, Event(22, day(4, 1), EventKind.ISSUE, -10)],
		}
		links = [CostLink(10, "B", 11, 50), CostLink(20, "C", 21, 50)]
		backdated = Event(30, day(1, 15), EventKind.RECEIPT, 40, 8)
		streams["A"].append(backdated)

		result = propagate(streams, links, ("A", backdated), FIFO)

		assert result.invalidations == (("A", 30), ("B", 11), ("C", 21))
		assert [event.id for event in result.re_realized_events] == [11, 21]
		assert close(result.streams["C"][0].declared_rate, 9.2)
		assert close(result.refolds["C"].final.qty, 40)
		assert close(result.refolds["C"].final.value, 40 * 9.2)
		for key in result.refolds:
			assert result.refolds[key].final == replay(result.streams[key], FIFO).final

	def test_reconciliation_between_backdate_and_transfer_cuts_propagation(self) -> None:
		"""Cut 1: the trigger refold converges at the assertion; the link never fires."""
		a = [
			Event(1, day(1, 1), EventKind.RECEIPT, 30, 10),
			Event(2, day(1, 20), EventKind.RECEIPT, 70, 12),
			Event(5, day(1, 25), EventKind.ASSERTION, assert_qty=100, assert_rate=11),
			Event(10, day(2, 1), EventKind.ISSUE, -50),
		]
		b_in = Event(11, day(2, 1), EventKind.RECEIPT, 50, 11)
		backdated = Event(30, day(1, 15), EventKind.RECEIPT, 40, 8)
		streams = {"A": [*a, backdated], "B": [b_in]}
		links = [CostLink(10, "B", 11, 50)]

		result = propagate(streams, links, ("A", backdated), FIFO)

		assert result.invalidations == (("A", 30),)
		assert result.re_realized_events == ()
		assert "B" not in result.refolds
		assert result.streams["B"] == [b_in]
		assert result.refolds["A"].converged_at == 5
		assert 10 in result.refolds["A"].skipped

	def test_backdate_landing_after_consumed_range_does_not_fire(self) -> None:
		"""Cut 2: the refold reaches the transfer but its consumed rate is unchanged."""
		a = [
			Event(1, day(1, 1), EventKind.RECEIPT, 60, 10),
			Event(10, day(2, 1), EventKind.ISSUE, -50),
		]
		b_in = Event(11, day(2, 1), EventKind.RECEIPT, 50, 10)
		backdated = Event(30, day(1, 15), EventKind.RECEIPT, 40, 12)
		streams = {"A": [*a, backdated], "B": [b_in]}
		links = [CostLink(10, "B", 11, 50)]

		result = propagate(streams, links, ("A", backdated), FIFO)

		assert 10 in result.refolds["A"].effects
		assert close(result.refolds["A"].effects[10].consumed_rate, 10)
		assert result.re_realized_events == ()
		assert result.invalidations == (("A", 30),)
		assert result.streams["B"] == [b_in]

	def test_extra_cost_and_cost_share_respected_on_re_realization(self) -> None:
		"""A repack: 25 out of the 50 consumed units' cost plus 100 operating cost."""
		streams, links = transfer_scenario(target_qty=25, cost_share_qty=50, extra_cost=100)
		assert close(streams["WIP"][0].declared_rate, (10.8 * 50 + 100) / 25)
		backdated = Event(20, day(1, 15), EventKind.RECEIPT, 40, 8)
		streams["Stores"].append(backdated)

		result = propagate(streams, links, ("Stores", backdated), FIFO)

		expected_rate = (9.2 * 50 + 100) / 25
		assert close(result.streams["WIP"][0].declared_rate, expected_rate)
		assert close(result.refolds["WIP"].final.value, 25 * expected_rate)

	@given(transfer_graphs())
	def test_cascade_equivalence_after_propagate(self, scenario) -> None:
		"""Load-bearing: every refolded key equals a from-scratch replay of its stream."""
		streams, links, inserted, context = scenario
		result = propagate(streams, links, ("K0", inserted), context)
		for key, stream in result.streams.items():
			refold = result.refolds.get(key)
			if refold is None:
				continue
			full = replay(stream, context)
			assert refold.final == full.final
			for event_id, state in refold.states.items():
				assert full.states[event_id] == state

	@given(transfer_graphs(allow_diamond=True))
	def test_propagation_terminates_on_random_graphs(self, scenario) -> None:
		streams, links, inserted, context = scenario
		result = propagate(streams, links, ("K0", inserted), context)
		assert isinstance(result, PropagationResult)
		assert len(result.invalidations) <= 1 + len(links) * (len(links) + 1)
