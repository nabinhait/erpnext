"""Voucher-level fold: coupled legs share one computed cost (design doc §2.5)."""
from __future__ import annotations

import unittest

import math

from hypothesis import given
from hypothesis import strategies as st

from erpnext.stock.engine import (
	CostLinkedLeg,
	Fifo,
	FoldContext,
	Leg,
	State,
	Voucher,
	fold,
	fold_voucher,
	replay,
)
from erpnext.stock.engine.tests.factories import at, build, issue, receipt

FIFO = FoldContext(policy=Fifo())
SOURCE_KEYS = ("a", "b", "c")
DESTINATION_KEYS = ("x", "y", "z")


def stocked(ops: list[tuple]) -> State:
	return replay(build(ops), FIFO).final


def transfer_in(
	leg_id: int, key: str, qty: float, cost_from: int,
	extra_cost: float = 0.0, cost_share_qty: float | None = None,
) -> CostLinkedLeg:
	return CostLinkedLeg(key, leg_id, at(60), qty, cost_from, extra_cost, cost_share_qty)


class TestTransferVoucher(unittest.TestCase):
	def test_transfer_moves_cost_without_a_declared_rate(self) -> None:
		"""Doc Example 3: [100@10] in Stores; transfer 50 to WIP at cost 10."""
		states = {"stores": stocked([("receipt", 100, 10)])}
		voucher = Voucher((
			Leg("stores", issue(10, 60, 50)),
			transfer_in(11, "wip", 50, cost_from=10),
		))
		result = fold_voucher(states, voucher, FIFO)
		assert (result.states["stores"].qty, result.states["stores"].value) == (50, 500)
		assert (result.states["wip"].qty, result.states["wip"].value) == (50, 500)
		assert result.realized_legs[-1].event.declared_rate == 10
		assert states == {"stores": stocked([("receipt", 100, 10)])}

	def test_repack_absorbs_full_cost_plus_extra(self) -> None:
		"""Consume 50 (cost 500) + operating cost 100 -> 25 finished @ 24."""
		states = {"stores": stocked([("receipt", 100, 10)])}
		voucher = Voucher((
			Leg("stores", issue(10, 60, 50)),
			transfer_in(11, "finished", 25, cost_from=10, extra_cost=100, cost_share_qty=50),
		))
		result = fold_voucher(states, voucher, FIFO)
		assert result.realized_legs[-1].event.declared_rate == 24
		assert (result.states["finished"].qty, result.states["finished"].value) == (25, 600)

	def test_chained_transfer_folds_in_dependency_order(self) -> None:
		"""A->B->C in one voucher: B's inflow folds before B's outflow."""
		states = {"a": stocked([("receipt", 100, 10)])}
		voucher = Voucher((
			Leg("a", issue(10, 60, 50)),
			transfer_in(11, "b", 50, cost_from=10),
			Leg("b", issue(12, 60, 50)),
			transfer_in(13, "c", 50, cost_from=12),
		))
		result = fold_voucher(states, voucher, FIFO)
		assert (result.states["c"].qty, result.states["c"].value) == (50, 500)
		assert result.states["b"] == State()
		assert result.states["a"].value == 500
		assert [leg.id for leg in result.realized_legs] == [10, 11, 12, 13]

	def test_split_transfer_shares_cost_by_qty(self) -> None:
		states = {"a": stocked([("receipt", 100, 10)])}
		voucher = Voucher((
			Leg("a", issue(10, 60, 50)),
			transfer_in(11, "b", 30, cost_from=10),
			transfer_in(12, "c", 20, cost_from=10),
		))
		result = fold_voucher(states, voucher, FIFO)
		assert (result.states["b"].qty, result.states["b"].value) == (30, 300)
		assert (result.states["c"].qty, result.states["c"].value) == (20, 200)

	def test_per_key_contexts(self) -> None:
		states = {"a": stocked([("receipt", 10, 5)])}
		voucher = Voucher((
			Leg("a", issue(10, 60, 10)),
			transfer_in(11, "b", 10, cost_from=10),
		))
		contexts = {"a": FIFO, "b": FoldContext(policy=Fifo(), fallback_rate=3)}
		result = fold_voucher(states, voucher, contexts)
		assert result.states["b"].value == 50


class TestVoucherValidation(unittest.TestCase):
	def test_cost_from_must_exist(self) -> None:
		voucher = Voucher((transfer_in(11, "b", 5, cost_from=99),))
		with self.assertRaisesRegex(ValueError, "outgoing leg"):
			fold_voucher({}, voucher, FIFO)

	def test_cost_from_cannot_reference_an_inward_leg(self) -> None:
		voucher = Voucher((
			Leg("a", receipt(10, 60, 5, 10)),
			transfer_in(11, "b", 5, cost_from=10),
		))
		with self.assertRaisesRegex(ValueError, "outgoing leg"):
			fold_voucher({}, voucher, FIFO)

	def test_cost_from_cannot_reference_a_cost_linked_leg(self) -> None:
		voucher = Voucher((
			Leg("a", issue(10, 60, 5)),
			transfer_in(11, "b", 5, cost_from=10),
			transfer_in(12, "c", 5, cost_from=11),
		))
		with self.assertRaisesRegex(ValueError, "outgoing leg"):
			fold_voucher({"a": stocked([("receipt", 10, 4)])}, voucher, FIFO)

	def test_swap_within_one_voucher_is_cyclic(self) -> None:
		states = {"a": stocked([("receipt", 10, 4)]), "b": stocked([("receipt", 10, 6)])}
		voucher = Voucher((
			Leg("a", issue(10, 60, 5)),
			transfer_in(11, "b", 5, cost_from=10),
			Leg("b", issue(12, 60, 5)),
			transfer_in(13, "a", 5, cost_from=12),
		))
		with self.assertRaisesRegex(ValueError, "cyclic"):
			fold_voucher(states, voucher, FIFO)

	def test_cost_shares_cannot_exceed_outgoing_qty(self) -> None:
		voucher = Voucher((
			Leg("a", issue(10, 60, 50)),
			transfer_in(11, "b", 30, cost_from=10),
			transfer_in(12, "c", 30, cost_from=10),
		))
		with self.assertRaisesRegex(ValueError, "exceed"):
			fold_voucher({"a": stocked([("receipt", 100, 10)])}, voucher, FIFO)

	def test_duplicate_leg_ids_rejected(self) -> None:
		voucher = Voucher((
			Leg("a", issue(10, 60, 5)),
			transfer_in(10, "b", 5, cost_from=10),
		))
		with self.assertRaisesRegex(ValueError, "duplicate"):
			fold_voucher({}, voucher, FIFO)

	def test_cost_linked_leg_must_be_inward(self) -> None:
		with self.assertRaisesRegex(ValueError, "inward"):
			CostLinkedLeg("b", 11, at(60), -5, cost_from=10)


@st.composite
def transfer_scenarios(draw) -> tuple[dict[str, State], Voucher]:
	"""Random prior states plus a pure multi-leg transfer voucher (no extra cost)."""
	states = {key: _prior_state(draw, minimum_receipts=1 if key == "a" else 0)
		for key in SOURCE_KEYS + DESTINATION_KEYS}
	legs: list[Leg | CostLinkedLeg] = []
	next_id = 100
	for key in SOURCE_KEYS:
		held = int(states[key].qty)
		if held == 0 or (key != "a" and not draw(st.booleans())):
			continue
		qty = draw(st.integers(1, held))
		source_id = next_id
		legs.append(Leg(key, issue(source_id, 60, qty)))
		next_id += 1
		for share_key, share_qty in _split_shares(draw, qty):
			legs.append(CostLinkedLeg(share_key, next_id, at(60), share_qty, source_id))
			next_id += 1
	return states, Voucher(tuple(legs))


def _prior_state(draw, minimum_receipts: int) -> State:
	receipts = draw(st.lists(
		st.tuples(st.integers(1, 100), st.integers(1, 50)),
		min_size=minimum_receipts, max_size=3,
	))
	return stocked([("receipt", qty, rate) for qty, rate in receipts])


def _split_shares(draw, qty: int) -> list[tuple[str, int]]:
	count = draw(st.integers(1, min(3, qty)))
	keys = draw(st.permutations(list(DESTINATION_KEYS)))[:count]
	shares, remaining = [], qty
	for index, key in enumerate(keys):
		left = count - index - 1
		share = draw(st.integers(1, remaining - left)) if left else remaining
		shares.append((key, share))
		remaining -= share
	return shares


def close(a: float, b: float) -> bool:
	return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


class TestVoucherFunctions(unittest.TestCase):
	@given(transfer_scenarios())
	def test_pure_transfer_is_value_neutral(self, scenario) -> None:
		"""Doc §2.8: a transfer moves value, it never creates or destroys it."""
		states, voucher = scenario
		result = fold_voucher(states, voucher, FIFO)
		before = sum(state.value for state in states.values())
		after = sum(state.value for state in result.states.values())
		assert close(before, after)

	@given(transfer_scenarios())
	def test_voucher_conserves_qty(self, scenario) -> None:
		states, voucher = scenario
		result = fold_voucher(states, voucher, FIFO)
		before = sum(state.qty for state in states.values())
		after = sum(state.qty for state in result.states.values())
		moved = sum(leg.event.qty_change for leg in result.realized_legs)
		assert close(after - before, moved) and close(moved, 0)

	@given(transfer_scenarios())
	def test_fold_voucher_matches_sequential_fold_of_realized_events(self, scenario) -> None:
		"""The realized events are self-contained facts: replaying them per key
		with the plain fold reproduces fold_voucher exactly."""
		states, voucher = scenario
		result = fold_voucher(states, voucher, FIFO)
		replayed = dict(states)
		effects = []
		for leg in result.realized_legs:
			state, effect = fold(replayed.get(leg.key, State()), leg.event, FIFO)
			replayed[leg.key] = state
			effects.append(effect)
		assert replayed == result.states
		assert tuple(effects) == result.effects
