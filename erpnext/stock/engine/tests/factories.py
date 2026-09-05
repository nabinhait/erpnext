"""Event builders shared by the test suite."""
from __future__ import annotations

from datetime import datetime, timedelta

from erpnext.stock.engine import Allocation, Event, EventKind, LotType

BASE = datetime(2025, 1, 1)  # noqa: DTZ001 — business posting time is naive by design
SPACING_MINUTES = 10


def at(minute: int) -> datetime:
	return BASE + timedelta(minutes=minute)


def receipt(event_id: int, minute: int, qty: float, rate: float) -> Event:
	return Event(event_id, at(minute), EventKind.RECEIPT, qty_change=qty, declared_rate=rate)


def issue(event_id: int, minute: int, qty: float) -> Event:
	return Event(event_id, at(minute), EventKind.ISSUE, qty_change=-qty)


def assertion(event_id: int, minute: int, qty: float, rate: float) -> Event:
	return Event(event_id, at(minute), EventKind.ASSERTION, assert_qty=qty, assert_rate=rate)


def reversal(
	event_id: int, minute: int, reverses: int, qty_change: float, rate: float | None = None
) -> Event:
	return Event(
		event_id, at(minute), EventKind.REVERSAL,
		qty_change=qty_change, declared_rate=rate, reverses_event=reverses,
	)


def lot_receipt(
	event_id: int, minute: int, lot_type: LotType, lots: list[tuple[str, float, float]]
) -> Event:
	"""lots: (lot_id, qty, rate) triples; qty must be 1 for serials."""
	allocations = tuple(Allocation(lot_type, lot, qty, rate) for lot, qty, rate in lots)
	total = sum(qty for _, qty, _ in lots)
	blended = sum(qty * rate for _, qty, rate in lots) / total
	return Event(
		event_id, at(minute), EventKind.RECEIPT,
		qty_change=total, declared_rate=blended, allocations=allocations,
	)


def lot_issue(
	event_id: int, minute: int, lot_type: LotType, lots: list[tuple[str, float]]
) -> Event:
	"""lots: (lot_id, qty) pairs; qty must be 1 for serials."""
	allocations = tuple(Allocation(lot_type, lot, -qty) for lot, qty in lots)
	total = sum(qty for _, qty in lots)
	return Event(
		event_id, at(minute), EventKind.ISSUE, qty_change=-total, allocations=allocations)


def build(ops: list[tuple], start_id: int = 1, start_minute: int = 0) -> list[Event]:
	"""ops: ('receipt', qty, rate) | ('issue', qty) | ('assertion', qty, rate).

	Events are spaced SPACING_MINUTES apart so tests can insert between them.
	"""
	events = []
	for offset, op in enumerate(ops):
		event_id = start_id + offset
		minute = start_minute + offset * SPACING_MINUTES
		if op[0] == "receipt":
			events.append(receipt(event_id, minute, op[1], op[2]))
		elif op[0] == "issue":
			events.append(issue(event_id, minute, op[1]))
		elif op[0] == "assertion":
			events.append(assertion(event_id, minute, op[1], op[2]))
		else:
			raise ValueError(f"unknown op {op[0]}")
	return events
