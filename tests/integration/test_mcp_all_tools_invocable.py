"""Every registered MCP tool is invocable THROUGH the FastMCP server (``server.call_tool``) and returns a
valid structured response — not merely that its underlying Python function works.

This is the silent-failure class the user flagged: a tool's function can be unit-green while its MCP
registration / schema / serialization wiring is broken, so it passes tests but FAILS the moment a client
calls it as an MCP tool. Function-level tests (and the existing surface tests, which only check
``list_tools`` NAMES) miss this. This test drives each tool through ``call_tool`` and asserts:

  • a FAST tool (no prereq, or a bad-id/bad-name probe) returns a structured dict (success OR a graceful
    ``{"error": str}``) and NEVER an unhandled crash;
  • a tool whose only valid call would RUN a workflow / need an LLM is probed with INVALID input and MUST
    reject it with a proper FastMCP validation error (proving registration + schema validation are wired).

Every tool in the pinned ``EXPECTED`` surface is covered exactly once across the two layers.
"""

from __future__ import annotations

import asyncio
import json
import os

from apecx_integration.mcp_surface.server import build_server
from tests.unit.test_mcp_tool_surface import EXPECTED

_WS = "/Users/onarykov/Downloads/apecx-cowork"
# Give the tools their prereqs so they actually WORK (not just degrade). Absent prereqs are fine — the
# tools degrade loudly to {"error": ...}, which this test also accepts; setting them exercises the live path.
os.environ.setdefault("APECX_WORKSPACE_ROOT", _WS)
os.environ.setdefault("APECX_DATA_ROOT", f"{_WS}/data")
os.environ.setdefault("APECX_SYNONYM_DICT_PATH", f"{_WS}/dictionary.sqlite")
os.environ.setdefault("APECX_LLM_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("APECX_LLM_MODEL", "nemotron-3-nano:4b")
os.environ.setdefault("APECX_LLM_API_KEY", "EMPTY")


def _invoke(server, name: str, args: dict):
    """Call a tool THROUGH the server. Returns (result_dict | None, error_type | None). A FastMCP exception
    (input-validation / prereq rejection) is a PROPER MCP error response — captured as error_type, not a
    crash. ``call_tool`` returns either a content list or a (content, structured) tuple (version-dependent)."""
    try:
        result = asyncio.run(server.call_tool(name, args))
    except (
        Exception
    ) as e:  # FastMCP ToolError et al. — a proper MCP error response, not a Python crash
        return None, type(e).__name__
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1], None
    content = result[0] if isinstance(result, tuple) else result
    if content and hasattr(content[0], "text"):
        try:
            return json.loads(content[0].text), None
        except Exception:
            return {"_text": content[0].text}, None
    return {}, None


# Layer 1: a fast valid response OR a graceful-error trigger — NEVER a full slow run.
_FAST_PROBES = {
    "list_workflows": {},
    "describe_workflow": {"name": "viral_epitope_analysis"},
    "inspect_workflow": {"name": "viral_epitope_analysis"},
    "apecx_context": {},
    "apecx_capabilities": {},
    "infrastructure_status": {},
    "database_statistics": {},
    "run_workflow": {"name": "__nonexistent_workflow__"},  # graceful 'unknown workflow'
    "inspect_run": {"run_id": "__nonexistent_run__"},  # graceful 'unknown run_id'
    "approve_design": {"token": "__nonexistent_token__"},  # graceful 'unknown token'
    "rhea_muscle_alignment": {},  # graceful 'prerequisites not met' when rhea is down
}
# Layer 2: a valid call would RUN a workflow / need an LLM — probe with INVALID input and require a proper
# FastMCP validation rejection (registration + schema work; the full functional path has its own test).
_VALIDATION_PROBES = {
    "compose_workflow": {"description": ""},
    "harmonized_search": {"term": "", "index": ""},
    "viral_epitope_analysis": {},  # missing required `query`
    "rag_e2e_synthesis": {},  # missing required `query`
}


def test_mcp_surface_covers_every_tool_exactly_once():
    """The probe map must cover the WHOLE pinned surface — no tool silently skipped."""
    covered = set(_FAST_PROBES) | set(_VALIDATION_PROBES)
    assert covered == EXPECTED, (
        f"probe map drifted from the surface. uncovered={sorted(EXPECTED - covered)}; "
        f"stale={sorted(covered - EXPECTED)}"
    )
    assert not (set(_FAST_PROBES) & set(_VALIDATION_PROBES))  # no tool in both layers


def test_every_tool_is_registered_and_invocable_via_mcp():
    server = build_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == EXPECTED, (
        f"surface drift: added={sorted(names - EXPECTED)} removed={sorted(EXPECTED - names)}"
    )

    # Layer 1 — fast tools: a structured dict, never an unhandled crash.
    for name, args in _FAST_PROBES.items():
        result, err = _invoke(server, name, args)
        assert err is None, (
            f"{name}: invoking via MCP raised unexpectedly ({err}) — broken MCP tool"
        )
        assert isinstance(result, dict), f"{name}: returned non-dict via MCP: {result!r}"
        if "error" in result:
            assert isinstance(result["error"], str) and result["error"], (
                f"{name}: empty/non-str error"
            )

    # Layer 2 — validation tools: invalid input MUST be rejected with a proper MCP error (registration +
    # schema validation are wired), not silently accepted.
    for name, args in _VALIDATION_PROBES.items():
        result, err = _invoke(server, name, args)
        assert err is not None or (isinstance(result, dict) and "error" in result), (
            f"{name}: invalid input was NOT rejected (got {result!r}) — schema validation not wired"
        )
