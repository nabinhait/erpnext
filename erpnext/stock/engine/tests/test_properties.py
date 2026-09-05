"""Property-based tests — rung 1 of the testing ladder (design doc §2.14)."""
from __future__ import annotations

import unittest

import math

from hypothesis import given
from hypothesis import strategies as st

from erpnext.stock.engine import Fifo, FoldContext, MovingAverage, refold_after_insert, replay
from erpnext.stock.engine.tests.factories import SPACING_MINUTES, assertion, build, receipt, reversal

FIFO = FoldContext(policy=Fifo())


@st.composite
def histories(draw, min_size=1, max_size=25, initial_held=0, allow_assertions=True):
	"""Op sequences whose issues never exceed the held quantity."""
	ops, held = [], initial_held
	for _ in range(draw(st.integers(min_size, max_size))):
		kinds = ["receipt"]
		if held > 0:
			kinds.append("issue")
		if allow_assertions:
			kinds.append("assertion")
		kind = draw(st.sampled_from(kinds))
		if kind == "receipt":
			qty, rate = draw(st.integers(1, 100)), draw(st.integers(1, 50))
			ops.append(("receipt", qty, rate))
			held += qty
		elif kind == "issue":
			qty = draw(st.integers(1, held))
			ops.append(("issue", qty))
			held -= qty
		else:
			qty, rate = draw(st.integers(0, 150)), draw(st.integers(1, 50))
			ops.append(("assertion", qty, rate))
			held = qty
	return ops


def reference_fifo(ops: list) -> tuple[float, float]:
	queue: list[list[float]] = []
	for op in ops:
		if op[0] == "receipt":
			queue.append([op[1], op[2]])
		elif op[0] == "assertion":
			queue = [[op[1], op[2]]] if op[1] else []
		else:
			need = op[1]
			while need:
				take = min(need, queue[0][0])
				queue[0][0] -= take
				need -= take
				if not queue[0][0]:
					queue.pop(0)
	return sum(q for q, _ in queue), sum(q * r for q, r in queue)


def close(a: float, b: float) -> bool:
	return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


class TestPropertiesFunctions(unittest.TestCase):
	@given(histories())
	def test_fifo_matches_reference(self, ops) -> None:
		result = replay(build(ops), FIFO)
		qty, value = reference_fifo(ops)
		assert close(result.final.qty, qty) and close(result.final.value, value)

	@given(histories(allow_assertions=False))
	def test_moving_average_matches_reference(self, ops) -> None:
		result = replay(build(ops), FoldContext(policy=MovingAverage()))
		qty = value = 0.0
		for op in ops:
			if op[0] == "receipt":
				qty, value = qty + op[1], value + op[1] * op[2]
			else:
				qty, value = qty - op[1], value - op[1] * (value / qty)
		assert close(result.final.qty, qty) and close(result.final.value, value)

	@given(histories(min_size=2), st.data())
	def test_checkpoint_resume_equals_full_fold(self, ops, data) -> None:
		events = build(ops)
		cut = data.draw(st.integers(1, len(events) - 1))
		full = replay(events, FIFO)
		prefix = replay(events[:cut], FIFO)
		resumed = replay(events[cut:], FIFO, start=prefix.final)
		assert resumed.final == full.final

	@given(histories(min_size=2), st.data())
	def test_incremental_refold_equals_full_refold(self, ops, data) -> None:
		"""Convergence detection never stops early wrongly."""
		events = build(ops)
		prior = replay(events, FIFO)
		slot = data.draw(st.integers(0, len(events) - 1))
		qty, rate = data.draw(st.integers(1, 100)), data.draw(st.integers(1, 50))
		inserted = receipt(9999, slot * SPACING_MINUTES + 5, qty, rate)
		events.append(inserted)
		incremental = refold_after_insert(events, inserted, prior, FIFO)
		full = replay(events, FIFO)
		merged = {**prior.states, **incremental.states}
		for event in events:
			assert full.states[event.id] == merged[event.id]

	@given(histories(), histories(), st.integers(0, 100), st.integers(1, 50), st.data())
	def test_assertion_erases_path_dependence(self, ops_a, ops_b, assert_qty, assert_rate, data) -> None:
		suffix = data.draw(
			histories(min_size=0, max_size=10, initial_held=assert_qty, allow_assertions=False))

		def run(prefix_ops: list) -> object:
			events = build(prefix_ops)
			events.append(assertion(500, 5000, assert_qty, assert_rate))
			events += build(suffix, start_id=501, start_minute=5010)
			return replay(events, FIFO).final

		assert run(ops_a) == run(ops_b)

	@given(histories(), st.integers(1, 100), st.integers(1, 50))
	def test_immediate_receipt_reversal_is_identity(self, ops, qty, rate) -> None:
		events = build(ops)
		before = replay(events, FIFO).final
		next_id = len(events) + 1
		minute = len(events) * SPACING_MINUTES
		events.append(receipt(next_id, minute, qty, rate))
		events.append(reversal(next_id + 1, minute + 1, reverses=next_id, qty_change=-qty))
		assert replay(events, FIFO).final == before

	@given(st.lists(
		st.one_of(
			st.tuples(st.just("receipt"), st.integers(1, 50), st.integers(1, 30)),
			st.tuples(st.just("issue"), st.integers(1, 80)),
		),
		min_size=1, max_size=25,
	))
	def test_fold_is_total_and_conserves_qty(self, ops) -> None:
		"""Issues may exceed held stock; the fold never raises and qty stays conserved."""
		result = replay(build(ops), FoldContext(policy=Fifo(), fallback_rate=7))
		net = sum(op[1] if op[0] == "receipt" else -op[1] for op in ops)
		assert close(result.final.qty, net)

	@given(histories())
	def test_effect_deltas_sum_to_final_value(self, ops) -> None:
		result = replay(build(ops), FIFO)
		total = sum(effect.value_delta for effect in result.effects.values())
		assert close(total, result.final.value)
