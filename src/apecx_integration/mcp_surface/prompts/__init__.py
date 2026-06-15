"""MCP-surface prompt assets for the desktop reasoning host (design §11, D7).

The connected host (Claude Desktop / IDE) is the orchestrating + synthesizing LLM in
desktop locus; these are the live MCP prompts it fetches to drive the reuse-first
analysis loop:
- ``rules_core.md``         → the ``reasoning_rules`` prompt (lean imperative rules).
- ``reasoning_protocol.md`` → the ``reasoning_protocol`` prompt (match → parametrize → execute).

The rules are the single canonical source (§11); the nine ``nanobrain-*`` skills carry
the rationale + scaffolding. ``test_reasoning_prompts`` keeps the rule markers pinned so a
prompt edit cannot silently drop a load-bearing imperative.

Ported from ``reasoning-agent-surface`` and ADAPTED to main's current tool surface: the
MATCH step uses the shipped discovery tools (``apecx_capabilities`` / ``list_workflows`` /
``describe_workflow``) and names ``compose_workflow`` as the generate-last fallback. The
branch's richer ``find_workflow`` / ``generate_workflow`` lifecycle is not on main yet (it
is the deferred agent-locus generate arc); re-point these prompts at it when it lands.
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Read a prompt asset by filename (e.g. ``rules_core.md``). FAIL-LOUD if absent —
    a missing prompt is a packaging bug, not something to paper over with a default."""
    path = _PROMPT_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"MCP prompt asset {name!r} not found at {path}. The desktop reasoning face "
            f"cannot serve it."
        )
    return path.read_text(encoding="utf-8")


__all__ = ["load_prompt"]
