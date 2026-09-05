"""Example-based tests, including the design doc's worked examples (Part 3)."""
from __future__ import annotations

import unittest

from erpnext.stock.engine import (
	Fifo,
	FoldContext,
	Lifo,
	MovingAverage,
	StandardCost,
	State,
	fold,
	refold_after_insert,
	replay,
)
from erpnext.stock.engine.event import Event, EventKind
from erpnext.stock.engine.tests.factories import at, build, issue, receipt, reversal

FIFO = FoldContext(policy=Fifo())


class TestWorkedExample(unittest.TestCase):
	"""Doc Part 3, Example 1: the backdated receipt."""

	def history(self) -> list:
		return build([
			("receipt", 100, 10),
			("issue", 60),
			("receipt", 50, 12),
			("issue", 70),
			("assertion", 20, 12),
			("issue", 5),
		])

	def test_initial_fold(self) -> None:
		result = replay(self.history(), FIFO)
		assert (result.states[4].qty, result.states[4].value) == (20, 240)
		assert result.effects[4].value_delta == -760
		assert (result.final.qty, result.final.value) == (15, 180)

	def test_backdated_receipt_converges_at_the_assertion(self) -> None:
		events = self.history()
		prior = replay(events, FIFO)
		backdated = receipt(7, 5, 20, 11)
		events.append(backdated)
		result = refold_after_insert(events, backdated, prior, FIFO)
		assert result.converged_at == 5
		assert result.skipped == (6,)
		assert result.effects[7].value_delta == 220
		assert result.effects[4].value_delta == -740
		assert result.effects[5].value_delta == -240


class TestNegativeExposure(unittest.TestCase):
	"""Doc Part 3, Example 4: issue before the covering receipt arrives."""

	def context(self) -> FoldContext:
		return FoldContext(policy=Fifo(), fallback_rate=10)

	def test_issue_from_empty_creates_exposure(self) -> None:
		state, effect = fold(State(), issue(1, 0, 5), self.context())
		assert (state.qty, state.value) == (-5, -50)
		assert effect.negative and effect.consumed_rate == 10

	def test_covering_receipt_books_true_up(self) -> None:
		exposed, _ = fold(State(), issue(1, 0, 5), self.context())
		state, effect = fold(exposed, receipt(2, 10, 20, 12), self.context())
		assert (state.qty, state.value) == (15, 180)
		assert effect.true_up == 10
		assert effect.value_delta == 230
		assert state.exposure_qty == 0


class TestReversal(unittest.TestCase):
	def test_reversing_a_receipt_removes_its_own_layer(self) -> None:
		events = build([("receipt", 100, 10), ("receipt", 50, 12)])
		before = replay(events[:1], FIFO).final
		events.append(reversal(3, 20, reverses=2, qty_change=-50))
		assert replay(events, FIFO).final == before

	def test_reversing_an_issue_restores_qty_and_value(self) -> None:
		events = build([("receipt", 100, 10), ("issue", 60)])
		result = replay(events, FIFO)
		events.append(reversal(3, 20, reverses=2, qty_change=60, rate=result.effects[2].consumed_rate))
		final = replay(events, FIFO).final
		assert (final.qty, final.value) == (100, 1000)


class TestPolicies(unittest.TestCase):
	def test_lifo_consumes_newest_first(self) -> None:
		events = build([("receipt", 100, 10), ("receipt", 50, 12), ("issue", 60)])
		result = replay(events, FoldContext(policy=Lifo()))
		assert result.effects[3].value_delta == -(50 * 12 + 10 * 10)
		assert result.final.qty == 90 and result.final.value == 900

	def test_moving_average_blends_receipts(self) -> None:
		events = build([("receipt", 100, 10), ("receipt", 50, 16), ("issue", 60)])
		result = replay(events, FoldContext(policy=MovingAverage()))
		assert result.effects[3].consumed_rate == 12
		assert result.final.qty == 90 and result.final.value == 1080

	def test_standard_cost_reports_variance(self) -> None:
		events = build([("receipt", 100, 11), ("issue", 50)])
		result = replay(events, FoldContext(policy=StandardCost(standard_rate=10)))
		assert result.effects[1].variance == 100
		assert result.effects[2].value_delta == -500

	def test_assertion_resets_state(self) -> None:
		events = build([("receipt", 100, 10), ("assertion", 40, 12)])
		final = replay(events, FIFO).final
		assert (final.qty, final.value) == (40, 480)
		assert final.layers[0].source_event_id == 2


class TestFoldFunctions(unittest.TestCase):
	def test_issue_with_declared_rate_consumes_matching_layers_first(self) -> None:
		"""A cost-linked leg (transit consumption, purchase return) declares the
		rate of the stock it moves; layers at that rate go first, not FIFO order."""
		events = [
			receipt(1, 0, qty=120, rate=0),
			receipt(2, 10, qty=120, rate=100),
			Event(3, at(20), EventKind.ISSUE, qty_change=-120, declared_rate=100),
		]
		result = replay(events, FoldContext(policy=Fifo()))
		final = result.final
		assert final.qty == 120
		assert final.value == 0  # the zero-rate layer stayed; the linked layer left
		assert result.effects[3].value_delta == -12000

	def test_revaluation_uplifts_source_layers_and_downstream_consumption(self) -> None:
		"""A landed-cost style revision sits at the receipt's instant and uplifts
		its layers; consumption folded after it trues up automatically."""
		events = [
			receipt(1, 0, qty=10, rate=100),
			Event(9, at(0), EventKind.REVALUATION, reverses_event=1, value_change=200),
			issue(2, 10, qty=4),
		]
		result = replay(events, FoldContext(policy=Fifo()))
		assert result.effects[9].value_delta == 200
		assert result.effects[9].qty_after == 10
		# consumption at the uplifted rate 120
		assert result.effects[2].value_delta == -480
		assert result.final.qty == 6
		assert result.final.value == 720

	def test_revaluation_with_nothing_surviving_is_a_noop(self) -> None:
		events = [
			receipt(1, 0, qty=5, rate=100),
			issue(2, 10, qty=5),
			Event(9, at(20), EventKind.REVALUATION, reverses_event=1, value_change=300),
		]
		result = replay(events, FoldContext(policy=Fifo()))
		assert result.effects[9].value_delta == 0
		assert result.final.value == 0
