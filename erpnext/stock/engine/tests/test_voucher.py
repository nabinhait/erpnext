"""Voucher-level fold: coupled legs share one cost pool (design doc §2.5)."""

from __future__ import annotations

import math
import unittest

from hypothesis import given
from hypothesis import strategies as st

from erpnext.stock.engine import (
	CostAllocation,
	CostPool,
	EngineContext,
	Fifo,
	Leg,
	PooledLeg,
	State,
	Voucher,
	apply_event,
	apply_voucher,
	replay,
)
from erpnext.stock.engine.tests.factories import at, build, issue, receipt

FIFO = EngineContext(policy=Fifo())
SOURCE_KEYS = ("a", "b", "c")
DESTINATION_KEYS = ("x", "y", "z")


def stocked(ops: list[tuple]) -> State:
	return replay(build(ops), FIFO).final


def output(
	leg_id: int,
	key: str,
	qty: float,
	fixed_rate: float | None = None,
	weight: float | None = None,
) -> PooledLeg:
	return PooledLeg(key, leg_id, at(60), qty, fixed_rate, weight)


def pool(sources: tuple[int, ...], outputs: tuple[PooledLeg, ...], extra_cost: float = 0.0) -> CostPool:
	return CostPool(sources, outputs, extra_cost)


def rate_of(result, leg_id: int) -> float:
	return next(leg.event.declared_rate for leg in result.realized_legs if leg.id == leg_id)


class TestPooledVoucher(unittest.TestCase):
	def test_transfer_moves_cost_without_a_declared_rate(self) -> None:
		"""Doc Example 3: [100@10] in Stores; transfer 50 to WIP at cost 10."""
		states = {"stores": stocked([("receipt", 100, 10)])}
		voucher = Voucher((Leg("stores", issue(10, 60, 50)),), (pool((10,), (output(11, "wip", 50),)),))
		result = apply_voucher(states, voucher, FIFO)
		assert (result.states["stores"].qty, result.states["stores"].value) == (50, 500)
		assert (result.states["wip"].qty, result.states["wip"].value) == (50, 500)
		assert rate_of(result, 11) == 10
		assert states == {"stores": stocked([("receipt", 100, 10)])}

	def test_repack_absorbs_full_cost_plus_extra(self) -> None:
		"""Consume 50 (cost 500) + operating cost 100 -> 25 finished @ 24."""
		states = {"stores": stocked([("receipt", 100, 10)])}
		voucher = Voucher(
			(Leg("stores", issue(10, 60, 50)),),
			(pool((10,), (output(11, "finished", 25),), extra_cost=100),),
		)
		result = apply_voucher(states, voucher, FIFO)
		assert rate_of(result, 11) == 24
		assert (result.states["finished"].qty, result.states["finished"].value) == (25, 600)

	def test_manufacture_pools_every_raw_material_and_holds_scrap_at_its_own_rate(self) -> None:
		"""RM1 500 + RM2 320 + labour 200 = 1020; scrap keeps 50, FG1 absorbs 970."""
		states = {"rm1": stocked([("receipt", 10, 100)]), "rm2": stocked([("receipt", 20, 40)])}
		voucher = Voucher(
			(Leg("rm1", issue(10, 60, 5)), Leg("rm2", issue(11, 60, 8))),
			(
				pool(
					(10, 11),
					(output(12, "fg", 4), output(13, "scrap", 2, fixed_rate=25)),
					extra_cost=200,
				),
			),
		)
		result = apply_voucher(states, voucher, FIFO)
		assert rate_of(result, 12) == 242.5
		assert rate_of(result, 13) == 25
		assert result.states["fg"].value == 970 and result.states["scrap"].value == 50

	def test_multiple_finished_goods_split_the_residual_by_weight(self) -> None:
		"""Two finished items, 60/40 by declared value, not by quantity."""
		states = {"rm": stocked([("receipt", 10, 100)])}
		voucher = Voucher(
			(Leg("rm", issue(10, 60, 10)),),
			(pool((10,), (output(11, "fg1", 4, weight=60), output(12, "fg2", 2, weight=40))),),
		)
		result = apply_voucher(states, voucher, FIFO)
		assert (result.states["fg1"].value, result.states["fg2"].value) == (600, 400)
		assert (rate_of(result, 11), rate_of(result, 12)) == (150, 200)

	def test_unpack_splits_one_item_into_several_at_different_rates(self) -> None:
		"""Disassembly: one FG worth 305 unpacks 60/40 across two parts."""
		states = {"fg": stocked([("receipt", 4, 305)])}
		voucher = Voucher(
			(Leg("fg", issue(10, 60, 1)),),
			(pool((10,), (output(11, "part_a", 2, weight=60), output(12, "part_b", 1, weight=40))),),
		)
		result = apply_voucher(states, voucher, FIFO)
		assert (rate_of(result, 11), rate_of(result, 12)) == (91.5, 122)

	def test_manufacture_consuming_and_producing_in_one_warehouse(self) -> None:
		"""The FG returns to the warehouse its raw material left: an ordinary
		voucher, not a cycle — the source only waits on *other* pools' inflows."""
		states = {"stores": stocked([("receipt", 100, 10)])}
		voucher = Voucher(
			(Leg("stores", issue(10, 60, 50)),),
			(pool((10,), (output(11, "stores", 25),), extra_cost=100),),
		)
		result = apply_voucher(states, voucher, FIFO)
		assert [leg.id for leg in result.realized_legs] == [10, 11]
		assert (result.states["stores"].qty, result.states["stores"].value) == (75, 1100)

	def test_chained_transfer_folds_in_dependency_order(self) -> None:
		"""A->B->C in one voucher: B's inflow folds before B's outflow."""
		states = {"a": stocked([("receipt", 100, 10)])}
		voucher = Voucher(
			(Leg("a", issue(10, 60, 50)), Leg("b", issue(12, 60, 50))),
			(pool((10,), (output(11, "b", 50),)), pool((12,), (output(13, "c", 50),))),
		)
		result = apply_voucher(states, voucher, FIFO)
		assert (result.states["c"].qty, result.states["c"].value) == (50, 500)
		assert result.states["b"] == State()
		assert result.states["a"].value == 500
		assert [leg.id for leg in result.realized_legs] == [10, 11, 12, 13]

	def test_split_transfer_shares_cost_by_qty(self) -> None:
		states = {"a": stocked([("receipt", 100, 10)])}
		voucher = Voucher(
			(Leg("a", issue(10, 60, 50)),),
			(pool((10,), (output(11, "b", 30), output(12, "c", 20))),),
		)
		result = apply_voucher(states, voucher, FIFO)
		assert (result.states["b"].qty, result.states["b"].value) == (30, 300)
		assert (result.states["c"].qty, result.states["c"].value) == (20, 200)

	def test_per_key_contexts(self) -> None:
		states = {"a": stocked([("receipt", 10, 5)])}
		voucher = Voucher((Leg("a", issue(10, 60, 10)),), (pool((10,), (output(11, "b", 10),)),))
		contexts = {"a": FIFO, "b": EngineContext(policy=Fifo(), fallback_rate=3)}
		result = apply_voucher(states, voucher, contexts)
		assert result.states["b"].value == 50


class TestVoucherValidation(unittest.TestCase):
	def test_source_must_exist(self) -> None:
		voucher = Voucher((), (pool((99,), (output(11, "b", 5),)),))
		with self.assertRaisesRegex(ValueError, "outgoing leg"):
			apply_voucher({}, voucher, FIFO)

	def test_source_cannot_reference_an_inward_leg(self) -> None:
		voucher = Voucher((Leg("a", receipt(10, 60, 5, 10)),), (pool((10,), (output(11, "b", 5),)),))
		with self.assertRaisesRegex(ValueError, "outgoing leg"):
			apply_voucher({}, voucher, FIFO)

	def test_source_cannot_reference_a_pooled_leg(self) -> None:
		voucher = Voucher(
			(Leg("a", issue(10, 60, 5)),),
			(pool((10,), (output(11, "b", 5),)), pool((11,), (output(12, "c", 5),))),
		)
		with self.assertRaisesRegex(ValueError, "outgoing leg"):
			apply_voucher({"a": stocked([("receipt", 10, 4)])}, voucher, FIFO)

	def test_swap_within_one_voucher_is_cyclic(self) -> None:
		states = {"a": stocked([("receipt", 10, 4)]), "b": stocked([("receipt", 10, 6)])}
		voucher = Voucher(
			(Leg("a", issue(10, 60, 5)), Leg("b", issue(12, 60, 5))),
			(pool((10,), (output(11, "b", 5),)), pool((12,), (output(13, "a", 5),))),
		)
		with self.assertRaisesRegex(ValueError, "cyclic"):
			apply_voucher(states, voucher, FIFO)

	def test_a_source_cannot_feed_two_pools(self) -> None:
		voucher = Voucher(
			(Leg("a", issue(10, 60, 50)),),
			(pool((10,), (output(11, "b", 30),)), pool((10,), (output(12, "c", 20),))),
		)
		with self.assertRaisesRegex(ValueError, "more than one cost pool"):
			apply_voucher({"a": stocked([("receipt", 100, 10)])}, voucher, FIFO)

	def test_duplicate_leg_ids_rejected(self) -> None:
		voucher = Voucher((Leg("a", issue(10, 60, 5)),), (pool((10,), (output(10, "b", 5),)),))
		with self.assertRaisesRegex(ValueError, "duplicate"):
			apply_voucher({}, voucher, FIFO)

	def test_pooled_leg_must_be_inward(self) -> None:
		with self.assertRaisesRegex(ValueError, "inward"):
			PooledLeg("b", 11, at(60), -5)

	def test_a_pool_needs_an_output(self) -> None:
		with self.assertRaisesRegex(ValueError, "at least one output"):
			CostPool((10,), ())

	def test_every_output_fixed_leaves_the_residual_unabsorbed(self) -> None:
		voucher = Voucher(
			(Leg("a", issue(10, 60, 50)),),
			(pool((10,), (output(11, "b", 30, fixed_rate=5),)),),
		)
		with self.assertRaisesRegex(ValueError, "absorbs the residual"):
			apply_voucher({"a": stocked([("receipt", 100, 10)])}, voucher, FIFO)

	def test_a_rule_that_loses_value_is_rejected(self) -> None:
		"""The invariant that makes custom rules safe: out must equal in."""

		class HalfOfIt(CostAllocation):
			def allocate(self, pool_value, outputs):
				return tuple(pool_value / len(outputs) / 2 for _ in outputs)

		voucher = Voucher(
			(Leg("a", issue(10, 60, 50)),),
			(CostPool((10,), (output(11, "b", 30),), rule=HalfOfIt()),),
		)
		with self.assertRaisesRegex(ValueError, "out of a pool of"):
			apply_voucher({"a": stocked([("receipt", 100, 10)])}, voucher, FIFO)


@st.composite
def pooled_vouchers(draw) -> tuple[dict[str, State], Voucher]:
	"""Random prior states plus a multi-pool voucher (transfers, no extra cost)."""
	states = {
		key: _prior_state(draw, minimum_receipts=1 if key == "a" else 0)
		for key in SOURCE_KEYS + DESTINATION_KEYS
	}
	legs: list[Leg] = []
	pools: list[CostPool] = []
	next_id = 100
	for key in SOURCE_KEYS:
		held = int(states[key].qty)
		if held == 0 or (key != "a" and not draw(st.booleans())):
			continue
		qty = draw(st.integers(1, held))
		source_id = next_id
		legs.append(Leg(key, issue(source_id, 60, qty)))
		next_id += 1
		outputs = []
		for share_key, share_qty in _split_shares(draw, qty):
			outputs.append(output(next_id, share_key, share_qty))
			next_id += 1
		pools.append(pool((source_id,), tuple(outputs)))
	return states, Voucher(tuple(legs), tuple(pools))


def _prior_state(draw, minimum_receipts: int) -> State:
	receipts = draw(
		st.lists(
			st.tuples(st.integers(1, 100), st.integers(1, 50)),
			min_size=minimum_receipts,
			max_size=3,
		)
	)
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
	@given(pooled_vouchers())
	def test_pure_transfer_is_value_neutral(self, scenario) -> None:
		"""Doc §2.8: a transfer moves value, it never creates or destroys it."""
		states, voucher = scenario
		result = apply_voucher(states, voucher, FIFO)
		before = sum(state.value for state in states.values())
		after = sum(state.value for state in result.states.values())
		assert close(before, after)

	@given(pooled_vouchers())
	def test_voucher_conserves_qty(self, scenario) -> None:
		states, voucher = scenario
		result = apply_voucher(states, voucher, FIFO)
		before = sum(state.qty for state in states.values())
		after = sum(state.qty for state in result.states.values())
		moved = sum(leg.event.qty_change for leg in result.realized_legs)
		assert close(after - before, moved) and close(moved, 0)

	@given(pooled_vouchers())
	def test_every_pool_hands_out_exactly_what_went_in(self, scenario) -> None:
		"""The allocation invariant, per pool: Sigma(output value) == pool."""
		states, voucher = scenario
		result = apply_voucher(states, voucher, FIFO)
		events = {leg.id: leg.event for leg in result.realized_legs}
		deltas = {effect.event_id: effect.value_delta for effect in result.effects}
		for cost_pool in voucher.pools:
			went_in = sum(-deltas[source] for source in cost_pool.sources) + cost_pool.extra_cost
			came_out = sum(
				events[out.id].qty_change * events[out.id].declared_rate for out in cost_pool.outputs
			)
			assert close(went_in, came_out)

	@given(pooled_vouchers())
	def test_fold_voucher_matches_sequential_fold_of_realized_events(self, scenario) -> None:
		"""The realized events are self-contained facts: replaying them per key
		with the plain fold reproduces apply_voucher exactly."""
		states, voucher = scenario
		result = apply_voucher(states, voucher, FIFO)
		replayed = dict(states)
		effects = []
		for leg in result.realized_legs:
			state, effect = apply_event(replayed.get(leg.key, State()), leg.event, FIFO)
			replayed[leg.key] = state
			effects.append(effect)
		assert replayed == result.states
		assert tuple(effects) == result.effects
