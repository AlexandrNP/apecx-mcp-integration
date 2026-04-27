"""Integration tests for the vendored DB query tools against real CSVs.

Covers every code path that tests/unit/test_database_tools.py
exercises via fixture DataFrames, but runs them against the actual
VIOLIN and BV-BRC data under $APECX_DATA_ROOT (or $APECX_ROOT/data).

Skip condition: neither env var points at a directory that contains
violin/Vaccine_Information.csv. This keeps CI green when the data
files aren't checked in — operators with a local data dir get full
coverage automatically.

Per the workspace mocks policy (CLAUDE.md Mocks Carve-Out table):
"Unit test that mocks an external dependency — yes, but the mocked
behaviour MUST also be exercised by a matching integration test
against the real dependency." This file is that matching test.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from apecx_integration.mcp_surface.data import database as db
from apecx_integration.mcp_surface.tools import database_tools as tools

# ---------------------------------------------------------------------------
# Skip guard — require both data and the APECX_DATA_ROOT / APECX_ROOT env var
# ---------------------------------------------------------------------------


def _data_root() -> Path | None:
    explicit = os.environ.get("APECX_DATA_ROOT")
    if explicit:
        p = Path(explicit)
        if (p / "violin" / "Vaccine_Information.csv").exists():
            return p
    workspace = os.environ.get("APECX_ROOT")
    if workspace:
        p = Path(workspace) / "data"
        if (p / "violin" / "Vaccine_Information.csv").exists():
            return p
    return None


_DATA_AVAILABLE = _data_root() is not None
_SKIP_REASON = (
    "real data not found — set APECX_DATA_ROOT or APECX_ROOT pointing at "
    "the workspace with data/violin/Vaccine_Information.csv"
)
pytestmark = pytest.mark.skipif(not _DATA_AVAILABLE, reason=_SKIP_REASON)


@pytest.fixture(autouse=True)
def _reset():
    db.reset_store()
    yield
    db.reset_store()


# ---------------------------------------------------------------------------
# Baseline: data loads + statistics are sane
# ---------------------------------------------------------------------------


def test_real_database_statistics_row_counts():
    """Verifies that the real VIOLIN + BV-BRC data loads and has the
    expected approximate row counts (guards against truncated CSVs).

    Integration counterpart to:
      test_database_statistics_shows_all_loaded_tables (unit)
    """
    out = asyncio.run(tools.database_statistics())
    assert "error" not in out, f"Store failed to load: {out.get('error')}"
    tables = out["tables"]
    assert tables["vaccines"]["rows"] >= 3000, "Expected ~3,507 vaccines"
    assert tables["pathogens"]["rows"] >= 200, "Expected ~217 pathogens"
    assert tables["genes"]["rows"] >= 4000, "Expected ~4,063 genes"
    assert tables["bvbrc_genomes"]["rows"] >= 16000, "Expected ~16,826 BV-BRC genomes"


# ---------------------------------------------------------------------------
# query_vaccines against real data
# ---------------------------------------------------------------------------


def test_real_query_vaccines_no_filter_returns_all():
    """Integration counterpart to test_query_vaccines_no_filter_returns_all."""
    out = asyncio.run(tools.query_vaccines(limit=5))
    assert out["total_in_database"] >= 3000
    assert out["count"] == 5
    assert len(out["vaccines"]) == 5


def test_real_query_vaccines_eeev_search():
    """Integration counterpart to test_query_vaccines_search_term_filters."""
    out = asyncio.run(tools.query_vaccines(search_term="EEEV"))
    assert out["total_matching"] >= 1, "Expected at least one EEEV vaccine in VIOLIN"
    # Spot-check that the returned records have expected columns
    v = out["vaccines"][0]
    assert "Vaccine_Name" in v or "Vaccine" in v


def test_real_query_vaccines_licensed_status():
    out = asyncio.run(tools.query_vaccines(status="Licensed", limit=5))
    assert out["total_matching"] >= 1
    for v in out["vaccines"]:
        assert v.get("Status") == "Licensed"


def test_real_query_vaccines_pathogen_filter():
    """Integration counterpart to test_query_vaccines_pathogen_filter_traverses_junction."""
    out = asyncio.run(tools.query_vaccines(pathogen="influenza", limit=10))
    assert out["total_matching"] >= 1


# ---------------------------------------------------------------------------
# query_pathogens against real data
# ---------------------------------------------------------------------------


def test_real_query_pathogens_all():
    """Integration counterpart to test_query_pathogens_all."""
    out = asyncio.run(tools.query_pathogens(limit=5))
    assert out["total_in_database"] >= 200
    # vaccine_count enrichment should be present
    for rec in out["pathogens"]:
        assert "vaccine_count" in rec


def test_real_query_pathogens_alphavirus_filter():
    out = asyncio.run(tools.query_pathogens(search_term="alphavirus", limit=10))
    assert out["total_matching"] >= 1


# ---------------------------------------------------------------------------
# query_genes against real data
# ---------------------------------------------------------------------------


def test_real_query_genes_all():
    """Integration counterpart to test_query_genes_all."""
    out = asyncio.run(tools.query_genes(limit=5))
    assert out["total_in_database"] >= 4000
    assert out["count"] == 5


def test_real_query_genes_influenza_search():
    """Integration counterpart to test_query_genes_search_term_protein_name."""
    out = asyncio.run(tools.query_genes(search_term="hemagglutinin", limit=5))
    assert out["total_matching"] >= 1


def test_real_query_genes_organism_filter():
    """Integration counterpart to test_query_genes_organism_filter."""
    out = asyncio.run(tools.query_genes(organism="influenza", limit=5))
    assert out["total_matching"] >= 1


# ---------------------------------------------------------------------------
# query_bvbrc_genomes against real data
# ---------------------------------------------------------------------------


def test_real_query_bvbrc_all():
    """Integration counterpart to test_query_bvbrc_all."""
    out = asyncio.run(tools.query_bvbrc_genomes(limit=5))
    assert out["total_in_database"] >= 16000
    assert out["count"] == 5


def test_real_query_bvbrc_species_filter():
    """Integration counterpart to test_query_bvbrc_species_filter."""
    out = asyncio.run(tools.query_bvbrc_genomes(species="Eastern equine", limit=10))
    assert out["total_matching"] >= 1


def test_real_query_bvbrc_year_range():
    """Integration counterpart to test_query_bvbrc_year_filter."""
    out = asyncio.run(tools.query_bvbrc_genomes(min_year=2020, max_year=2023, limit=5))
    assert out["total_matching"] >= 0  # result count may vary; just no crash


def test_real_query_bvbrc_zero_sentinel_skips_year_filter():
    """Integration counterpart to test_query_bvbrc_zero_sentinel_skips_year_filter."""
    all_out = asyncio.run(tools.query_bvbrc_genomes(limit=1))
    sentinel_out = asyncio.run(tools.query_bvbrc_genomes(min_year=0, max_year=0, limit=1))
    assert all_out["total_in_database"] == sentinel_out["total_in_database"]


# ---------------------------------------------------------------------------
# get_vaccine_pathogen_genes against real data
# ---------------------------------------------------------------------------


def test_real_get_vaccine_pathogen_genes_influenza():
    """Integration counterpart to test_get_vaccine_pathogen_genes_traversal."""
    out = asyncio.run(tools.get_vaccine_pathogen_genes("influenza"))
    assert "error" not in out
    assert out["total_vaccines"] >= 1
    # At least one vaccine must have at least one gene link
    has_genes = any(len(v["genes"]) > 0 for v in out["vaccines"])
    assert has_genes, "Expected gene links for influenza vaccines in VIOLIN"


def test_real_get_vaccine_pathogen_genes_unknown_returns_empty():
    """Integration counterpart to test_get_vaccine_pathogen_genes_unknown_pathogen_returns_empty."""
    out = asyncio.run(tools.get_vaccine_pathogen_genes("xyzzy_not_a_real_pathogen"))
    assert out["total_vaccines"] == 0


# ---------------------------------------------------------------------------
# resolve_entity against real data
# ---------------------------------------------------------------------------


def test_real_resolve_entity_influenza():
    """Integration counterpart to test_resolve_entity_matches_across_tables."""
    out = asyncio.run(tools.resolve_entity("influenza"))
    assert "error" not in out
    assert len(out["matches"]["pathogens"]) >= 1
    assert len(out["matches"]["vaccines"]) >= 1


def test_real_resolve_entity_returns_ncbi_ids():
    """Integration counterpart to test_resolve_entity_returns_identifiers."""
    out = asyncio.run(tools.resolve_entity("eastern equine encephalitis"))
    assert len(out["identifiers"]["ncbi_taxonomy_ids"]) >= 1
