"""execution_locus — the single server flag that selects the orchestration face.

Lives in ``composition/runtime`` (NOT ``mcp_surface``) on purpose: composition steps must
READ the active locus to decide whether to synthesize internally or defer to the host, and
the layer order is ``mcp_surface → composition`` — a step importing up into ``mcp_surface``
would violate it. ``mcp_surface.locus`` re-exports this module (importing DOWN is fine).

ONE flag, ``desktop`` ↔ ``agent``, chosen at server startup. It selects WHO the
synthesizing LLM is — it gates no other feature.

- ``desktop`` (default): the host LLM (Claude Desktop) is the orchestrator + synthesizer.
  A workflow whose LLM is its FINAL synthesis omits that call and returns the assembled
  evidence for the host to synthesize (true inversion). A workflow with a genuine IN-DAG
  LLM step cannot be omitted — it resolves a configured local/external fallback LLM, or
  LOUDLY REFUSES (never silent/null). MCP sampling is unsupported by Claude Desktop
  (design D2), so the host can never serve as a sub-step LLM — hence fallback-or-refuse,
  not host-delegation, for in-DAG steps.
- ``agent``: the apecx server is the orchestrator; every LLM step (final OR in-DAG) uses
  the configured server LLM, exactly as headless ``run_workflow`` does today.

Surfaced as an MCP **startup flag**: ``apecx-mcp --locus desktop|agent`` →
``build_server(locus=...)``. ``$APECX_EXECUTION_LOCUS`` is a documented fallback; the
startup flag wins. ``set_active_locus`` records the resolved value process-wide so steps +
``run_workflow`` read it without a handle to the server object.

FAIL-LOUD on an unknown value: a typo'd locus must NOT silently fall back to ``desktop``.
"""

from __future__ import annotations

import os
from enum import StrEnum

ENV_VAR = "APECX_EXECUTION_LOCUS"


class ExecutionLocus(StrEnum):
    """The two orchestration faces. ``StrEnum`` so a value compares/serializes as plain
    text (``ExecutionLocus.DESKTOP == "desktop"``)."""

    DESKTOP = "desktop"
    AGENT = "agent"


def resolve_locus(value: str | None = None) -> ExecutionLocus:
    """Resolve the locus from ``value`` (the ``--locus`` flag) or ``$APECX_EXECUTION_LOCUS``.

    Default is ``desktop`` when unset/empty (the host-first product center). Raises
    ``ValueError`` (FAIL-LOUD) on any other value — never silent-defaults a typo to
    ``desktop`` (which would hide a misconfiguration).
    """
    raw = value if value is not None else os.environ.get(ENV_VAR)
    if raw is None or raw.strip() == "":
        return ExecutionLocus.DESKTOP
    try:
        return ExecutionLocus(raw.strip().lower())
    except ValueError:
        valid = ", ".join(repr(m.value) for m in ExecutionLocus)
        raise ValueError(
            f"Invalid execution locus {raw!r}: must be one of {valid}. Set it via "
            f"`apecx-mcp --locus <value>` (or ${ENV_VAR}). It is not silently defaulted, "
            f"to avoid hiding a misconfiguration."
        ) from None


# Process-wide active locus — set once by ``build_server`` from the resolved startup flag,
# read by steps + run_workflow (which have no handle to the FastMCP server object). Defaults
# to ``desktop`` until a server build sets it, so a direct step/test invocation is host-first.
_ACTIVE_LOCUS: ExecutionLocus = ExecutionLocus.DESKTOP


def set_active_locus(locus: ExecutionLocus) -> None:
    """Record the resolved locus process-wide (called by ``build_server``)."""
    global _ACTIVE_LOCUS
    _ACTIVE_LOCUS = locus


def get_active_locus() -> ExecutionLocus:
    """The locus this process is running under (``desktop`` until a server build sets it)."""
    return _ACTIVE_LOCUS


__all__ = [
    "ENV_VAR",
    "ExecutionLocus",
    "get_active_locus",
    "resolve_locus",
    "set_active_locus",
]
