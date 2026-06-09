"""HITL routing tests for the 6 database/canonical-entity MCP tools.

Each tool's contract since the 2026-06-09 ``_hitl_gate`` refactor:

  - empty term → tool runs without entity filter (bypass)
  - ambiguous term → tool returns the paused envelope verbatim WITHOUT
    touching the pandas data layer
  - resolved term → tool proceeds normally with the canonical IRI

These tests pin all three branches per tool by stubbing the gate and
the data layer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from apecx_integration.mcp_surface.tools import database_tools


def _fake_paused() -> dict:
    """The paused-envelope shape the gate emits for an ambiguous term."""
    return {
        "status": "paused_awaiting_disambiguation",
        "markdown": "### Term `RSV` is ambiguous (tool `X`)\n...",
        "next_action": {
            "kind": "re-invoke_with_chosen_iri",
            "tool": "X",
            "param_name": "search_term",
            "options": ["http://x/A", "http://x/B"],
        },
        "candidates": [
            {
                "canonical_iri": "http://x/A",
                "canonical_label": "A",
                "canonical_ontology": "n",
                "confidence": 1.0,
            },
            {
                "canonical_iri": "http://x/B",
                "canonical_label": "B",
                "canonical_ontology": "n",
                "confidence": 1.0,
            },
        ],
        "tool": "X",
        "term": "RSV",
    }


# A pandas-side fake that records whether the data layer was touched.
class _DataLayerSpy:
    def __init__(self):
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        return {"results": [{"id": 1}], "_spy_called": True}


@pytest.fixture
def fake_store():
    """Patch the pandas store lookup so the tools don't error on missing data."""
    with patch.object(database_tools, "_db") as mock_db:
        mock_db.get_store.return_value = ("fake_store", None)
        yield mock_db


# ─────────────────────────────────────────────────────────────────────────
# AMBIGUOUS path — each tool MUST return the paused envelope verbatim AND
# NOT call into the pandas data layer.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool_name,tool_fn,kwargs",
    [
        ("query_vaccines", database_tools.query_vaccines, {"search_term": "RSV"}),
        ("query_pathogens", database_tools.query_pathogens, {"search_term": "RSV"}),
        ("query_genes", database_tools.query_genes, {"search_term": "RSV"}),
        ("query_bvbrc_genomes", database_tools.query_bvbrc_genomes, {"search_term": "RSV"}),
        (
            "get_vaccine_pathogen_genes",
            database_tools.get_vaccine_pathogen_genes,
            {"pathogen_name": "RSV"},
        ),
        ("resolve_entity", database_tools.resolve_entity, {"name": "RSV"}),
    ],
)
def test_ambiguous_term_returns_paused_envelope_without_touching_data_layer(
    tool_name,
    tool_fn,
    kwargs,
    fake_store,
):
    paused = _fake_paused()
    with patch.object(database_tools, "resolve_with_hitl_gate", return_value=paused) as mock_gate:
        result = asyncio.run(tool_fn(**kwargs))

    # Tool returned the paused envelope verbatim.
    assert result is paused
    assert result["status"] == "paused_awaiting_disambiguation"

    # Critical: the gate was invoked with the right tool_name + param_name.
    mock_gate.assert_called_once()
    call_kwargs = mock_gate.call_args.kwargs
    assert call_kwargs["tool_name"] == tool_name
    # Each tool's gate param_name matches the tool's actual entity arg name.
    expected_param = (
        "pathogen_name"
        if tool_name == "get_vaccine_pathogen_genes"
        else "name"
        if tool_name == "resolve_entity"
        else "search_term"
    )
    assert call_kwargs["param_name"] == expected_param


# ─────────────────────────────────────────────────────────────────────────
# BYPASS path — empty search_term must NOT call the gate (no entity to
# disambiguate) and the tool proceeds with no entity filter.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool_fn,kwargs",
    [
        (database_tools.query_vaccines, {"search_term": ""}),
        (database_tools.query_pathogens, {"search_term": ""}),
        (database_tools.query_genes, {"search_term": ""}),
        (database_tools.query_bvbrc_genomes, {"search_term": ""}),
        (database_tools.get_vaccine_pathogen_genes, {"pathogen_name": ""}),
    ],
)
def test_empty_term_skips_gate_entirely(tool_fn, kwargs, fake_store):
    """When the user passes no entity, the gate isn't called at all."""
    # Stub each underlying data_layer fn the tools dispatch to.
    fake_store.query_vaccines.return_value = {"results": []}
    fake_store.query_pathogens.return_value = {"results": []}
    fake_store.query_genes.return_value = {"results": []}
    fake_store.query_bvbrc_genomes.return_value = {"results": []}
    fake_store.get_vaccine_pathogen_genes.return_value = {"results": []}

    with patch.object(database_tools, "resolve_with_hitl_gate") as mock_gate:
        asyncio.run(tool_fn(**kwargs))
        mock_gate.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# RESOLVED path — single-match term flows through to the data layer with
# the resolved IRI used as a precision filter (ncbi_taxonomy_id / vo_id /
# ncbi_gene_id depending on tool).
# ─────────────────────────────────────────────────────────────────────────


def _fake_resolved(
    canonical_iri: str = "http://purl.obolibrary.org/obo/NCBITaxon_37124",
    label: str = "Chikungunya virus",
    path: str = "fast",
    ncbi_taxonomy_id: int | None = 37124,
) -> dict:
    """The resolved-shape the gate emits for an unambiguous term."""
    from apecx_integration.synonym_dictionary.enums import ResolutionStatus
    from apecx_integration.synonym_dictionary.lookup import LookupResult

    lr = LookupResult(
        surface_form="CHIKV",
        path=path,
        canonical_iri=canonical_iri,
        canonical_label=label,
        canonical_ontology="ncbitaxon",
        confidence=1.0,
        resolution_status=ResolutionStatus.ID_ANCHORED,
        synonyms=(),
        evidence="",
    )
    return {
        "status": "resolved",
        "lookup_result": lr,
        "ncbi_taxonomy_id": ncbi_taxonomy_id,
        "resolution_meta": {
            "path": path,
            "canonical_iri": canonical_iri,
            "canonical_label": label,
            "canonical_ontology": "ncbitaxon",
            "confidence": 1.0,
            "resolution_status": "id_anchored",
            "evidence": "",
        },
    }


def test_resolved_term_passes_to_data_layer_with_canonical_filter(fake_store):
    """query_bvbrc_genomes with an unambiguous term: gate returns resolved,
    tool extracts ncbi_taxonomy_id, calls the data layer with it."""
    fake_store.query_bvbrc_genomes.return_value = {"results": [{"id": 1}]}
    with patch.object(database_tools, "resolve_with_hitl_gate", return_value=_fake_resolved()):
        result = asyncio.run(database_tools.query_bvbrc_genomes(search_term="CHIKV"))
    # Data layer WAS called.
    assert fake_store.query_bvbrc_genomes.called
    # The ncbi_taxonomy_id derived from the resolved IRI was passed through.
    call_kwargs = fake_store.query_bvbrc_genomes.call_args.kwargs
    assert call_kwargs["ncbi_taxonomy_id"] == 37124
    # Resolution metadata is surfaced to the caller for transparency.
    assert "_resolution" in result
    assert (
        result["_resolution"]["canonical_iri"] == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    )
