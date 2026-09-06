"""Valuation policies: how cost attaches to quantity within one stock key.

Policies see only layers and quantities. Negative-stock handling lives in the
fold; consume() is never asked for more than the layers hold.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .state import QTY_EPSILON, Layer

Layers = tuple[Layer, ...]


class ValuationPolicy(ABC):
	"""Strategy interface shared by all four methods."""

	merges_layers = False
	"""True when receipts collapse into one layer, so no layer remembers its receipt."""

	@abstractmethod
	def receive(self, layers: Layers, qty: float, rate: float, event_id: int) -> tuple[Layers, float]:
		"""Add inward qty at rate. Returns (new_layers, variance)."""

	@abstractmethod
	def consume(self, layers: Layers, qty: float) -> tuple[Layers, float]:
		"""Remove qty (<= total held). Returns (new_layers, cost)."""


class LayeredPolicy(ValuationPolicy):
	"""Shared mechanics for queue-of-layers policies (FIFO, LIFO)."""

	newest_first = False

	def receive(self, layers: Layers, qty: float, rate: float, event_id: int) -> tuple[Layers, float]:
		return layers + (Layer(qty, rate, event_id),), 0.0

	def consume(self, layers: Layers, qty: float) -> tuple[Layers, float]:
		remaining = list(layers)
		need, cost = qty, 0.0
		while need > 0 and remaining:
			index = len(remaining) - 1 if self.newest_first else 0
			head = remaining.pop(index)
			take = min(need, head.qty)
			cost += take * head.rate
			need -= take
			if head.qty - take > QTY_EPSILON:
				remaining.insert(index, Layer(head.qty - take, head.rate, head.source_event_id))
		return tuple(remaining), cost


class Fifo(LayeredPolicy):
	newest_first = False


class Lifo(LayeredPolicy):
	newest_first = True


class MergedLayerPolicy(ValuationPolicy):
	"""Shared mechanics for policies that keep one merged layer.

	Consuming merges first, so a state folded under a layered policy (the item's
	valuation method changed) is absorbed instead of rejected."""

	merges_layers = True

	def consume(self, layers: Layers, qty: float) -> tuple[Layers, float]:
		held = sum(layer.qty for layer in layers)
		rate = sum(layer.value for layer in layers) / held
		remaining = held - qty
		if remaining <= QTY_EPSILON:
			return (), qty * rate
		return (Layer(remaining, rate, layers[-1].source_event_id),), qty * rate


class MovingAverage(MergedLayerPolicy):
	"""One merged layer whose rate is the weighted average of everything received."""

	def receive(self, layers: Layers, qty: float, rate: float, event_id: int) -> tuple[Layers, float]:
		held = sum(layer.qty for layer in layers)
		value = sum(layer.value for layer in layers) + qty * rate
		total = held + qty
		return (Layer(total, value / total, event_id),), 0.0


class StandardCost(MergedLayerPolicy):
	"""Quantity at a fixed rate; the declared-vs-standard difference is a variance."""

	def __init__(self, standard_rate: float) -> None:
		self.standard_rate = standard_rate

	def receive(self, layers: Layers, qty: float, rate: float, event_id: int) -> tuple[Layers, float]:
		held = sum(layer.qty for layer in layers)
		variance = qty * (rate - self.standard_rate)
		return (Layer(held + qty, self.standard_rate, event_id),), variance
