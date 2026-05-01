"""Smoke tests for the resolve_canonical_entity MCP tool (P3.8).

Tests exercise the tool's public API in isolation — no real dictionary
artifact, no live OLS, no database store.  They verify:
- The tool is importable and callable.
- Empty input returns a well-formed error dict with resolution_path.
- Unknown entity_type returns a well-formed error dict.
- A valid call returns a LookupResult-shaped dict with all required keys.
- The tool degrades gracefully when no dictionary or database is configured.

Integration tests that exercise the fast path against a real built
dictionary belong in tests/integration/ (gated on APECX_SYNONYM_DICT_LIVE_OLS=1).
"""

from __future__ import annotations

import pytest

_REQUIRED_KEYS = frozenset(
    {
        "surface_form",
        "resolution_path",
        "canonical_iri",
        "canonical_label",
        "canonical_ontology",
        "confidence",
        "resolution_status",
        "synonyms",
        "evidence",
    }
)


@pytest.mark.asyncio
async def test_resolve_canonical_entity_importable_and_callable() -> None:
    from apecx_integration.mcp_surface.tools.canonical_entity import resolve_canonical_entity

    assert callable(resolve_canonical_entity)


@pytest.mark.asyncio
async def test_resolve_canonical_entity_empty_name_returns_error() -> None:
    from apecx_integration.mcp_surface.tools.canonical_entity import resolve_canonical_entity

    result = await resolve_canonical_entity("")
    assert "error" in result
    assert result.get("resolution_path") == "miss"


@pytest.mark.asyncio
async def test_resolve_canonical_entity_unknown_entity_type_returns_error() -> None:
    from apecx_integration.mcp_surface.tools.canonical_entity import resolve_canonical_entity

    result = await resolve_canonical_entity("Chikungunya", entity_type="bacterium")
    assert "error" in result
    assert "entity_type" in result["error"] or "unknown" in result["error"]


@pytest.mark.asyncio
async def test_resolve_canonical_entity_miss_returns_required_keys() -> None:
    """When no dictionary is loaded and no DB store, must still return a
    well-formed dict with ALL required keys so callers don't key-error."""
    from apecx_integration.mcp_surface.tools.canonical_entity import resolve_canonical_entity

    result = await resolve_canonical_entity("definitely not an entity xyz")
    assert isinstance(result, dict), "result must be a dict"
    missing = _REQUIRED_KEYS - result.keys()
    assert not missing, f"result missing required keys: {missing}"
    assert result["resolution_path"] in ("fast", "slow", "miss")
    assert result["canonical_iri"] is None
    assert result["confidence"] == 0.0


@pytest.mark.asyncio
async def test_resolve_canonical_entity_valid_entity_type_filter() -> None:
    """Supplying a valid entity_type should not error — it may still miss if
    no dictionary is loaded, but it must not raise or return a type-error."""
    from apecx_integration.mcp_surface.tools.canonical_entity import resolve_canonical_entity

    for etype in ("pathogen", "vaccine", "disease", "gene"):
        result = await resolve_canonical_entity("Chikungunya", entity_type=etype)
        assert "error" not in result or "entity_type" not in result.get(
            "error", ""
        ), f"Valid entity_type {etype!r} was rejected as unknown"
        assert "resolution_path" in result


@pytest.mark.asyncio
async def test_resolve_canonical_entity_registered_in_server() -> None:
    """The tool must be registered in the MCP server so it appears in the
    tool catalogue the model sees.  Verifies server.py wiring is correct."""
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    tool_names = {t.name for t in server._tool_manager.list_tools()}
    assert "resolve_canonical_entity" in tool_names, (
        "resolve_canonical_entity not registered in the MCP server.  "
        "Check that server.py calls server.tool()(canonical_entity_tools.resolve_canonical_entity)."
    )
