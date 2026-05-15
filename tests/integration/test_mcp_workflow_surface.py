"""Integration tests for the workflow-catalog MCP surface.

Two surfaces, mirroring test_rhea_muscle_alignment_workflow.py and
test_rag_e2e_workflow_yaml.py:

  1. **Unconditional** — ``build_server()`` loads the default catalog
     without crashing; ``tools/list`` includes ``rhea_muscle_alignment``;
     when its prereqs are unmet the tool is STILL listed but its
     description carries [UNAVAILABLE] and a call returns an actionable
     error (never a silent absence, never a silent success).

  2. **Gated on $RHEA_MCP_URL set AND rhea importable** — drive
     ``server.call_tool("rhea_muscle_alignment", {...})`` in-process and
     assert the alignment result.

The gated test auto-skips cleanly when its prerequisites aren't
present, so this file is safe to run in CI without a Rhea server.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os

import pytest

pytestmark = pytest.mark.integration


_RHEA_URL = os.environ.get("RHEA_MCP_URL")
_rhea_importable = (
    importlib.util.find_spec("rhea") is not None
    and importlib.util.find_spec("rhea.utils.proxy") is not None
)


# ---------------------------------------------------------------------------
# Unconditional — wire-shape assertions on the real FastMCP server
# ---------------------------------------------------------------------------


def test_build_server_loads_default_catalog_and_exposes_rhea_tool() -> None:
    """The packaged catalog produces a tool surface including
    rhea_muscle_alignment. The pre-fix path was a hardcoded
    ``align_sequences_with_muscle`` Python tool; that name MUST be gone."""
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "rhea_muscle_alignment" in names, (
        "expected rhea_muscle_alignment in tools/list; got " + repr(sorted(names))
    )
    assert "align_sequences_with_muscle" not in names, (
        "retired tool align_sequences_with_muscle leaked back into the surface — "
        "the catalog-driven replacement must be the only entry"
    )


def test_rhea_tool_input_schema_carries_catalog_properties() -> None:
    """The catalog declares fasta_path + fasta_text as the inputs; FastMCP
    must echo them into the tools/list inputSchema."""
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    rhea = next(t for t in tools if t.name == "rhea_muscle_alignment")
    schema = rhea.inputSchema
    assert isinstance(schema, dict)
    props = schema.get("properties") or {}
    assert "fasta_path" in props
    assert "fasta_text" in props


def test_rhea_tool_unavailable_when_prereqs_unmet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RHEA_MCP_URL is unset:
    - rhea_muscle_alignment remains in tools/list (no silent absence),
    - its description carries the [UNAVAILABLE:] marker,
    - a call_tool surfaces an actionable error (no silent success)."""
    monkeypatch.delenv("RHEA_MCP_URL", raising=False)
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    rhea = next((t for t in tools if t.name == "rhea_muscle_alignment"), None)
    assert rhea is not None, (
        "rhea_muscle_alignment must remain visible even when unavailable — "
        "silent absence is forbidden by the workflow-registry policy"
    )
    assert "[UNAVAILABLE:" in (rhea.description or ""), (
        "expected the [UNAVAILABLE:] marker in the tool description when prereqs "
        "are unmet; got: " + repr(rhea.description)
    )

    # Drive a call_tool through the real FastMCP path. The tool body
    # should return an actionable {"error": ...} envelope.
    result = asyncio.run(server.call_tool("rhea_muscle_alignment", {}))
    # FastMCP returns either a list of ContentBlocks (unstructured) or
    # a (sequence, dict-or-result) tuple depending on the structured
    # flag. We accept either shape and dig out the dict.
    if isinstance(result, tuple) and len(result) == 2:
        _content, body = result
    else:
        body = result

    # When body is a list of TextContent we have to parse the JSON.
    if isinstance(body, list):
        # ContentBlock list: take the first text block and parse it.
        text = None
        for block in body:
            text = getattr(block, "text", None)
            if text is not None:
                break
        assert text is not None, f"no text in call_tool result: {body!r}"
        payload = json.loads(text)
    elif isinstance(body, dict):
        payload = body
    else:
        raise AssertionError(f"unexpected call_tool result shape: {type(body)!r}")

    assert "error" in payload, (
        'tool body must surface a {"error": ...} envelope when prereqs unmet; '
        f"got payload={payload!r}"
    )
    assert "RHEA_MCP_URL" in payload["error"]


# ---------------------------------------------------------------------------
# Gated on $RHEA_MCP_URL — full end-to-end via call_tool
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    _RHEA_URL is None or not _rhea_importable,
    reason="RHEA_MCP_URL not set or rhea package not importable",
)
def test_rhea_tool_call_against_live_rhea() -> None:
    """Drive a real tools/call rhea_muscle_alignment with fasta_text
    against a live Rhea MCP server. No real network mocking — the
    FastMCP path is in-process but the Rhea call is real."""
    from apecx_integration.mcp_surface import workflow_registry
    from apecx_integration.mcp_surface.server import build_server

    workflow_registry._clear_workflow_cache()
    server = build_server()
    fasta = ">a\nMKTAYIAKQRQISFVKSHFSRQ\n>b\nMKTAYIAKQRQISFVKSHFSRQ\n"
    result = asyncio.run(server.call_tool("rhea_muscle_alignment", {"fasta_text": fasta}))

    if isinstance(result, tuple) and len(result) == 2:
        _content, body = result
    else:
        body = result
    if isinstance(body, list):
        text = next((getattr(b, "text", None) for b in body if getattr(b, "text", None)), None)
        assert text is not None
        payload = json.loads(text)
    else:
        payload = body

    assert isinstance(payload, dict)
    assert "error" not in payload, f"live-rhea call returned error: {payload.get('error')!r}"
    assert payload.get("n_sequences") == 2
    assert payload.get("alignment_length", 0) > 0
    assert payload.get("summary", "").strip()
