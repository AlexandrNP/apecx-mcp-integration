"""Decomposer mode flag (RoC-3a).

Two distinct modes of operation, selected by an MCP-settings env var (same pattern as the
server's ``APECX_MCP_AUTOSTART_*`` flags):

- ``plan_returner`` (DEFAULT) — return of control: match + propose a plan as a
  ``needs_input(decomposition_choice)``; the frontier LLM fills + sequences it. Safe default —
  autonomy is opt-in, never silent.
- ``auto_solver`` — bounded autonomous solving (match → dispatch → recurse → integrate, under
  hard depth/cost caps with a loud "cannot solve").

Both honor return-of-control (neither guesses parameter values).
"""

from __future__ import annotations

import os
from typing import Literal

DecomposerMode = Literal["auto_solver", "plan_returner"]

_VALID: tuple[DecomposerMode, ...] = ("auto_solver", "plan_returner")
_DEFAULT: DecomposerMode = "plan_returner"
_ENV = "APECX_EO_DECOMPOSER_MODE"


def resolve_decomposer_mode(explicit: str | None = None) -> DecomposerMode:
    """Resolve the mode from an explicit value or ``$APECX_EO_DECOMPOSER_MODE``.

    Defaults to ``plan_returner``. Raises loudly (never silently falls back) on an invalid value —
    a typo'd mode that silently ran the wrong behavior would be a silent failure.
    """
    raw = explicit if explicit is not None else os.environ.get(_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT
    mode = raw.strip().lower()
    if mode not in _VALID:
        raise ValueError(f"{_ENV} must be one of {_VALID}; got {raw!r}")
    return mode  # type: ignore[return-value]


__all__ = ["DecomposerMode", "resolve_decomposer_mode"]
