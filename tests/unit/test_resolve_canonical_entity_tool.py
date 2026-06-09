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
        assert "error" not in result or "entity_type" not in result.get("error", ""), (
            f"Valid entity_type {etype!r} was rejected as unknown"
        )
        assert "resolution_path" in result


@pytest.mark.asyncio
async def test_resolve_canonical_entity_NOT_registered_on_mcp_wire() -> None:
    """The tool was deregistered from the MCP surface on 2026-06-09 as part
    of the tier cleanup. Its first-line description ("Resolve a biomedical
    entity name to its canonical ontology IRI") competed with the
    ``harmonized_search`` workflow tool for the same LLM routing
    decisions, and LLMs were picking the cheaper-looking primitive — the
    canonical IRI is already returned as ``bundle.parts.resolution.canonical_iri``
    in every ``harmonized_search`` response, so the standalone tool was
    redundant.

    The Python function ``canonical_entity_tools.resolve_canonical_entity``
    remains importable for internal use (composer-generated workflows
    can call it directly); only the MCP wire registration was removed.
    """
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    tool_names = {t.name for t in server._tool_manager.list_tools()}
    assert "resolve_canonical_entity" not in tool_names, (
        "resolve_canonical_entity was deregistered on 2026-06-09 but is "
        "back on the MCP wire. If you intentionally restored it, update "
        "this test + the deregistration comment in server.py."
    )
    # Companion check: the 6 database primitives demoted in the same
    # cleanup must ALSO be absent from the wire. Pin the full
    # deregistration set so a future re-registration trips a single
    # well-named test.
    demoted = {
        "query_vaccines",
        "query_pathogens",
        "query_genes",
        "query_bvbrc_genomes",
        "get_vaccine_pathogen_genes",
        "resolve_entity",
        "resolve_canonical_entity",
    }
    leaked = demoted & tool_names
    assert not leaked, (
        f"tier-cleanup deregistration regressed — these tools are back "
        f"on the MCP wire: {sorted(leaked)}. Either intentional (update "
        f"this test) or accidental (remove the rogue server.tool() call)."
    )
