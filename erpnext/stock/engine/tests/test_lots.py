"""Lot sub-states: serial and batch valuation as the same fold at finer granularity."""
from __future__ import annotations

import unittest

import math

from hypothesis import given
from hypothesis import strategies as st

from erpnext.stock.engine import Allocation, Event, EventKind, Fifo, FoldContext, LotType, MovingAverage, replay
from erpnext.stock.engine.tests.factories import at, issue, lot_issue, lot_receipt, receipt

FIFO = FoldContext(policy=Fifo())
FALLBACK = FoldContext(policy=Fifo(), fallback_rate=10)


class TestSerial(unittest.TestCase):
	def test_specific_identification_needs_no_dedicated_code(self) -> None:
		events = [
			lot_receipt(1, 0, LotType.SERIAL, [("S1", 1, 10), ("S2", 1, 12), ("S3", 1, 14)]),
			lot_issue(2, 10, LotType.SERIAL, [("S2", 1)]),
		]
		result = replay(events, FIFO)
		assert result.effects[2].consumed_rate == 12
		assert result.final.qty == 2 and result.final.value == 24
		assert result.final.lot(LotType.SERIAL, "S2").qty == 0

	def test_issuing_a_missing_serial_is_modelled_exposure(self) -> None:
		events = [lot_issue(1, 0, LotType.SERIAL, [("S9", 1)])]
		result = replay(events, FALLBACK)
		assert result.effects[1].negative
		assert result.final.qty == -1 and result.final.value == -10
		assert result.final.lot(LotType.SERIAL, "S9").exposure_qty == 1

	def test_double_receipt_of_one_serial_is_corruption(self) -> None:
		events = [
			lot_receipt(1, 0, LotType.SERIAL, [("S1", 1, 10)]),
			lot_receipt(2, 10, LotType.SERIAL, [("S1", 1, 10)]),
		]
		with self.assertRaises(AssertionError):
			replay(events, FIFO)

	def test_serial_allocation_must_move_one_unit(self) -> None:
		with self.assertRaises(ValueError):
			lot_receipt(1, 0, LotType.SERIAL, [("S1", 2, 10)])


class TestBatch(unittest.TestCase):
	def test_batchwise_valuation_is_the_same_fold_per_lot(self) -> None:
		events = [
			lot_receipt(1, 0, LotType.BATCH, [("B1", 100, 10)]),
			lot_receipt(2, 10, LotType.BATCH, [("B2", 50, 20)]),
			lot_issue(3, 20, LotType.BATCH, [("B2", 30)]),
		]
		result = replay(events, FIFO)
		assert result.effects[3].consumed_rate == 20
		assert result.effects[3].value_delta == -600
		assert result.final.lot(LotType.BATCH, "B1").value == 1000
		assert result.final.lot(LotType.BATCH, "B2").qty == 20

	def test_negative_batch_gets_true_up_on_cover(self) -> None:
		events = [
			lot_issue(1, 0, LotType.BATCH, [("B1", 5)]),
			lot_receipt(2, 10, LotType.BATCH, [("B1", 20, 12)]),
		]
		result = replay(events, FALLBACK)
		assert result.effects[1].negative
		assert result.effects[2].true_up == 10
		assert result.final.qty == 15 and result.final.value == 180

	def test_allocations_may_cover_part_of_qty_change_but_never_exceed_it(self) -> None:
		template = lot_receipt(1, 0, LotType.BATCH, [("B1", 5, 10)])
		partial = Event(
			1, template.posting_datetime, template.kind,
			qty_change=99, declared_rate=10, allocations=template.allocations,
		)
		assert partial.qty_change == 99  # remainder is unlotted pool stock
		with self.assertRaises(ValueError):
			Event(
				1, template.posting_datetime, template.kind,
				qty_change=2, declared_rate=10, allocations=template.allocations,
			)


@st.composite
def batch_histories(draw, batches=("B1", "B2", "B3"), max_size=25):
	"""Single-batch ops whose issues never exceed that batch's held qty."""
	ops, held = [], dict.fromkeys(batches, 0)
	for _ in range(draw(st.integers(1, max_size))):
		batch = draw(st.sampled_from(batches))
		if held[batch] > 0 and draw(st.booleans()):
			qty = draw(st.integers(1, held[batch]))
			ops.append(("issue", batch, qty))
			held[batch] -= qty
		else:
			qty, rate = draw(st.integers(1, 100)), draw(st.integers(1, 50))
			ops.append(("receipt", batch, qty, rate))
			held[batch] += qty
	return ops


def _events_from(ops) -> list[Event]:
	events = []
	for index, op in enumerate(ops):
		if op[0] == "receipt":
			events.append(lot_receipt(index + 1, index * 10, LotType.BATCH, [(op[1], op[2], op[3])]))
		else:
			events.append(lot_issue(index + 1, index * 10, LotType.BATCH, [(op[1], op[2])]))
	return events


def _reference_fifo_for(ops, batch: str) -> tuple[float, float]:
	queue: list[list[float]] = []
	for op in ops:
		if op[1] != batch:
			continue
		if op[0] == "receipt":
			queue.append([op[2], op[3]])
		else:
			need = op[2]
			while need:
				take = min(need, queue[0][0])
				queue[0][0] -= take
				need -= take
				if not queue[0][0]:
					queue.pop(0)
	return sum(q for q, _ in queue), sum(q * r for q, r in queue)


def _close(a: float, b: float) -> bool:
	return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


class TestQuantityTagBatches(unittest.TestCase):
	"""Partially allocated events: flag-off batches are quantity tags, so their
	share reaches the engine unallocated and folds against the shared pool.
	The pools never borrow from each other."""

	MA = FoldContext(policy=MovingAverage())

	def test_pools_close_at_exactly_zero_on_stock_out(self) -> None:
		new = [("NEW", 10, 70.0)]
		events = [
			receipt(1, 0, 10, 50),  # flag-off batch: plain pool receipt
			lot_receipt(2, 10, LotType.BATCH, new),
			lot_issue(3, 20, LotType.BATCH, [("NEW", 2)]),
			issue(4, 30, 2),  # from the flag-off batch: shared pool
			issue(5, 40, 8),
			lot_issue(6, 50, LotType.BATCH, [("NEW", 8)]),
		]
		result = replay(events, self.MA)
		assert result.effects[3].consumed_rate == 70
		assert result.effects[4].consumed_rate == 50  # pool rate, not the 58.89 blend
		assert result.effects[5].consumed_rate == 50
		assert result.effects[6].consumed_rate == 70
		assert result.final.qty == 0 and result.final.value == 0

	def test_one_event_split_across_pool_and_lot(self) -> None:
		events = [
			receipt(1, 0, 10, 50),
			lot_receipt(2, 10, LotType.BATCH, [("NEW", 10, 70.0)]),
			Event(3, at(20), EventKind.ISSUE, qty_change=-4,
				allocations=(Allocation(LotType.BATCH, "NEW", -2),)),
		]
		result = replay(events, self.MA)
		assert _close(result.effects[3].consumed_rate, 60)  # 2@70 + 2@50
		assert _close(result.final.value, 8 * 50 + 8 * 70)
		assert _close(result.final.lot(LotType.BATCH, "NEW").qty, 8)

	def test_partial_receipt_layers_remainder_into_pool(self) -> None:
		event = Event(1, at(0), EventKind.RECEIPT, qty_change=5, declared_rate=70,
			allocations=(Allocation(LotType.BATCH, "NEW", 2, 70.0),))
		result = replay([event], self.MA)
		assert _close(result.final.value, 5 * 70)
		assert _close(result.final.lot(LotType.BATCH, "NEW").qty, 2)
		assert _close(sum(layer.qty for layer in result.final.layers), 3)

	def test_allocations_beyond_qty_change_are_rejected(self) -> None:
		with self.assertRaises(ValueError):
			Event(1, at(0), EventKind.ISSUE, qty_change=-1,
				allocations=(Allocation(LotType.BATCH, "B", -2),))
		with self.assertRaises(ValueError):
			Event(1, at(0), EventKind.RECEIPT, qty_change=5, declared_rate=10,
				allocations=(Allocation(LotType.BATCH, "B", -2),))


class TestBaselineAssertions(unittest.TestCase):
	"""Cutover freeze: an assertion pins legacy's stored balance — lots seeded,
	pool priced at assert_rate, negative balances frozen as exposure."""

	MA = FoldContext(policy=MovingAverage())

	def test_negative_assertion_freezes_exposure_settled_by_receipt(self) -> None:
		events = [
			Event(1, at(0), EventKind.ASSERTION, assert_qty=-3, assert_rate=80),
			receipt(2, 10, 10, 100),
		]
		result = replay(events, self.MA)
		assert result.effects[1].value_delta is not None  # frozen, not folded away
		assert _close(result.effects[2].true_up, 60)  # 3 uncovered units caught up at 100
		assert result.final.qty == 7 and _close(result.final.value, 700)
		assert result.final.exposure_qty == 0

	def test_assertion_seeds_lots_and_prices_pool_at_assert_rate(self) -> None:
		event = Event(
			1, at(0), EventKind.ASSERTION, assert_qty=10, assert_rate=50,
			allocations=(Allocation(LotType.BATCH, "NEW", 4, 70.0),),
		)
		result = replay([event], self.MA)
		assert _close(result.final.lot(LotType.BATCH, "NEW").value, 280)
		assert result.final.qty == 10 and _close(result.final.value, 6 * 50 + 280)

	def test_assertion_allocation_shape_is_validated(self) -> None:
		with self.assertRaises(ValueError):
			Event(
				1, at(0), EventKind.ASSERTION, assert_qty=-1, assert_rate=10,
				allocations=(Allocation(LotType.BATCH, "B", 1, 10.0),),
			)
		with self.assertRaises(ValueError):
			Event(
				1, at(0), EventKind.ASSERTION, assert_qty=3, assert_rate=10,
				allocations=(Allocation(LotType.BATCH, "B", 4, 10.0),),
			)


class TestRateBuckets(unittest.TestCase):
	"""Serial-wise valuation: an outward movement's rate_buckets consume the
	matching receipt layers, so each picked unit leaves at its own rate."""

	FIFO = FoldContext(policy=Fifo())

	def test_picked_units_leave_at_their_own_rates(self) -> None:
		events = [
			receipt(1, 0, 100, 50),
			receipt(2, 10, 100, 55),
			Event(3, at(20), EventKind.ISSUE, qty_change=-10,
				rate_buckets=((6, 50.0), (4, 55.0))),
		]
		result = replay(events, self.FIFO)
		assert _close(result.effects[3].value_delta, -(6 * 50 + 4 * 55))  # 520, not FIFO's 500
		assert _close(result.final.qty, 190)
		assert _close(result.final.value, 94 * 50 + 96 * 55)

	def test_unmatched_bucket_falls_back_to_policy(self) -> None:
		events = [
			receipt(1, 0, 10, 50),
			Event(2, at(10), EventKind.ISSUE, qty_change=-4,
				rate_buckets=((4, 99.0),)),  # no 99-layer exists
		]
		result = replay(events, self.FIFO)
		assert _close(result.effects[2].value_delta, -200)  # policy consumed at 50
		assert _close(result.final.value, 300)

	def test_bucket_shape_is_validated(self) -> None:
		with self.assertRaises(ValueError):
			Event(1, at(0), EventKind.RECEIPT, qty_change=5, declared_rate=10,
				rate_buckets=((5, 10.0),))
		with self.assertRaises(ValueError):
			Event(1, at(0), EventKind.ISSUE, qty_change=-2, rate_buckets=((3, 10.0),))


class TestLotsFunctions(unittest.TestCase):
	@given(batch_histories())
	def test_batch_fold_matches_independent_per_batch_reference(self, ops) -> None:
		result = replay(_events_from(ops), FIFO)
		total_qty = total_value = 0.0
		for batch in ("B1", "B2", "B3"):
			qty, value = _reference_fifo_for(ops, batch)
			lot = result.final.lot(LotType.BATCH, batch)
			assert _close(lot.qty, qty) and _close(lot.value, value)
			total_qty += qty
			total_value += value
		assert _close(result.final.qty, total_qty) and _close(result.final.value, total_value)

	@given(batch_histories(max_size=15), st.data())
	def test_mixed_lot_and_plain_stock_aggregate(self, ops, data) -> None:
		"""Untracked top-level stock and lot stock coexist on one key."""
		plain_qty, plain_rate = data.draw(st.integers(1, 100)), data.draw(st.integers(1, 50))
		events = _events_from(ops)
		events.append(receipt(900, 9000, plain_qty, plain_rate))
		result = replay(events, FIFO)
		lot_qty = sum(lot.state.qty for lot in result.final.lots)
		lot_value = sum(lot.state.value for lot in result.final.lots)
		assert _close(result.final.qty, lot_qty + plain_qty)
		assert _close(result.final.value, lot_value + plain_qty * plain_rate)
