"""Shared pass-through contract for the decomposed epitope-combination steps.

The ``epitope_combination_feasibility_assessment`` workflow is a linear pipeline
(``intake -> classify -> release -> envelope``). Any step may emit a TERMINAL payload
shaped to the ``EnvelopeStep`` contract — it carries a ``markdown`` key (an intake
miss, or a release withhold/approve). Downstream steps MUST forward such a payload
untouched rather than process it, so an early terminal is never clobbered. This module
centralizes the two pieces that contract needs, used by both ``classify`` and
``release``.
"""

from __future__ import annotations

from typing import Any

# A terminal payload (EnvelopeStep-shaped) is discriminated by this key. No intermediate
# (intake / classify) payload may carry it — that invariant keeps the check unambiguous.
TERMINAL_MARKER = "markdown"


def unwrap_single_key(input_data: dict[str, Any]) -> dict[str, Any]:
    """Descend a single-key trigger envelope (``{du_name: payload}``) generically.

    Field-AGNOSTIC on purpose: a terminal payload lacks the intermediate pipeline keys,
    so a key-specific unwrap would skip it and ``is_terminal`` below would miss. Returns
    ``input_data`` unchanged when it is not a single-key dict wrapper.

    Relies on an invariant of this pipeline: a real intake/classify payload carries
    several keys (never exactly one), so only the trigger envelope is a single-key dict.
    Keep intermediate payloads multi-key or this single-level unwrap would over-descend.
    """
    if len(input_data) == 1:
        only = next(iter(input_data.values()))
        if isinstance(only, dict):
            return only
    return input_data


def is_terminal(payload: dict[str, Any]) -> bool:
    """True when ``payload`` already carries a terminal ``EnvelopeStep``-shaped output."""
    return isinstance(payload, dict) and TERMINAL_MARKER in payload
