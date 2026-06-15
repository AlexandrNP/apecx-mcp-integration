"""Pin the desktop reasoning-host MCP prompts (rules_core.md / reasoning_protocol.md).

These are served live as the ``reasoning_rules`` / ``reasoning_protocol`` MCP prompts so the
connected host (Claude Desktop) can fetch its operating loop on demand. The tests guard two
silent-failure shapes:
  1. A prompt edit that drops a load-bearing imperative (REUSE-FIRST, CLOSED-CLASS, the
     framework rules) — the marker assertions catch it.
  2. A prompt that references a tool NOT on main's surface yet (``find_workflow`` /
     ``generate_workflow`` are the deferred agent-locus generate arc) — the host would be
     told to call a nonexistent tool. The dangling-ref assertion catches it.
"""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.mcp_surface.prompts import load_prompt


def test_load_prompt_fails_loud_on_missing_asset():
    with pytest.raises(FileNotFoundError, match="not found"):
        load_prompt("does_not_exist.md")


def test_rules_core_carries_the_load_bearing_imperatives():
    rules = load_prompt("rules_core.md")
    for needle in (
        "REUSE-FIRST RULE",
        "CLOSED-CLASS RULE",
        "from_config",
        "auto_transfer: true",
        "requires_llm",
    ):
        assert needle in rules, needle


def test_protocol_carries_the_three_phases():
    protocol = load_prompt("reasoning_protocol.md")
    for needle in ("MATCH", "PARAMETRIZE", "EXECUTE", "apecx_capabilities", "run_workflow"):
        assert needle in protocol, needle


@pytest.mark.parametrize("asset", ["rules_core.md", "reasoning_protocol.md"])
def test_prompts_reference_only_tools_on_main_surface(asset):
    """The prompts must NOT point the host at tools that don't exist on main yet — those are
    the deferred agent-locus generate arc. Generation guidance uses the shipped
    ``compose_workflow``."""
    text = load_prompt(asset)
    assert "find_workflow" not in text, f"{asset} references find_workflow (not on main)"
    assert "generate_workflow" not in text, f"{asset} references generate_workflow (not on main)"


def test_prompts_are_registered_on_the_server():
    """Both prompts must be live on the built server (fail-loud at build, not first fetch)."""
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    names = {p.name for p in asyncio.run(server.list_prompts())}
    assert {"reasoning_rules", "reasoning_protocol"} <= names
