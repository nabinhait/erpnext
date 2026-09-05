"""Frozen policy inputs. Resolved before folding starts; the fold itself does no I/O."""
from __future__ import annotations

from dataclasses import dataclass

from .policies import ValuationPolicy


@dataclass(frozen=True, slots=True)
class FoldContext:
	"""Everything the fold is allowed to know besides State and Event.

	fallback_rate values issues from an empty balance when no better provisional
	rate exists (the honest replacement for today's six-query rate cascade).
	"""

	policy: ValuationPolicy
	fallback_rate: float = 0.0
