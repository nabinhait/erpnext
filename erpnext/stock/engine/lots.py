"""Lots: serial numbers and batches as sub-keys of the stock key.

A serial number and a batch are the same construct at different granularity.
Lot-tracked stock runs the same fold within each lot; the item-warehouse
aggregate is the sum of the lot sub-states.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LotType(str, Enum):
	SERIAL = "Serial"
	BATCH = "Batch"


@dataclass(frozen=True, slots=True)
class Allocation:
	"""Child of an Event: how much of qty_change belongs to which lot.

	qty is signed and must be ±1 for serials. declared_rate (inward only)
	overrides the event's declared_rate for this lot.
	"""

	lot_type: LotType
	lot_id: str
	qty: float
	declared_rate: float | None = None
