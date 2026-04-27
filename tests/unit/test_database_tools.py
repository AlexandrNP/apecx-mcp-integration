"""Unit tests for the vendored DB query layer (Option B-1).

Uses in-process fixture DataFrames (no real CSV files). Per the
workspace mocks policy each fixture-covered code path must have a
matching integration test that runs it against real data — see
tests/integration/test_db_tools_real_data.py.
"""

from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from apecx_integration.mcp_surface.data import database as db
from apecx_integration.mcp_surface.tools import database_tools as tools


# ---------------------------------------------------------------------------
# Fixtures — realistic column sets matching the real VIOLIN/BV-BRC schemas
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_store():
    """Always reset the lazy singleton so tests don't bleed state."""
    db.reset_store()
    yield
    db.reset_store()


@pytest.fixture()
def small_store():
    """A pre-built DatabaseStore loaded from tiny in-process DataFrames."""
    vaccines_df = pd.DataFrame([
        {"id": 1, "Vaccine": "Vax-A", "Vaccine_Name": "Alpha Vaccine",
         "Type": "Subunit vaccine", "Status": "Licensed",
         "Antigen": "EEEV E2 protein", "Description": "Eastern equine encephalitis",
         "Tradename": None},
        {"id": 2, "Vaccine": "Vax-B", "Vaccine_Name": "Beta Vaccine",
         "Type": "Live attenuated vaccine", "Status": "Clinical trial",
         "Antigen": "Influenza HA", "Description": "Influenza prevention",
         "Tradename": "FluShot"},
        {"id": 3, "Vaccine": "Vax-C", "Vaccine_Name": "Gamma Vaccine",
         "Type": "Subunit vaccine", "Status": "Research",
         "Antigen": "Rubella E1", "Description": "Rubella disease",
         "Tradename": None},
    ])
    pathogens_df = pd.DataFrame([
        {"id": 10, "Pathogen": "Eastern equine encephalitis virus",
         "Disease": "Eastern equine encephalitis",
         "NCBI_Taxonomy_ID": 11021, "VIOLIN_c_pathogen_id": "P10",
         "Pathogen_Description": "Alphavirus causing EEEV encephalitis"},
        {"id": 11, "Pathogen": "Influenza A virus",
         "Disease": "Influenza",
         "NCBI_Taxonomy_ID": 11520, "VIOLIN_c_pathogen_id": "P11",
         "Pathogen_Description": "Orthomyxovirus"},
    ])
    genes_df = pd.DataFrame([
        {"id": 100, "Gene_Name": "E2", "Protein_Name": "Envelope glycoprotein E2",
         "Organism": "Eastern equine encephalitis virus",
         "Molecule_Role": "Structural protein"},
        {"id": 101, "Gene_Name": "HA", "Protein_Name": "Hemagglutinin",
         "Organism": "Influenza A virus",
         "Molecule_Role": "Surface glycoprotein"},
    ])
    vaccine_pathogen_df = pd.DataFrame([
        {"id": 1000, "vaccine_id": 1, "pathogen_id": 10},
        {"id": 1001, "vaccine_id": 2, "pathogen_id": 11},
    ])
    gene_vaccine_pathogen_df = pd.DataFrame([
        {"id": 2000, "vaccine_pathogen_id": 1000, "gene_id": 100},
        {"id": 2001, "vaccine_pathogen_id": 1001, "gene_id": 101},
    ])
    bvbrc_df = pd.DataFrame([
        {"Genome ID": "G1", "Genome Name": "EEEV strain X", "Species": "Eastern equine encephalitis virus",
         "Strain": "North American", "Host Name": "Homo sapiens",
         "Host Common Name": "human", "Isolation Country": "United States",
         "Geographic Location": "USA", "Collection Year": 2010,
         "Other Names": None},
        {"Genome ID": "G2", "Genome Name": "CHIKV isolate Y", "Species": "Chikungunya virus",
         "Strain": "Asian", "Host Name": "Aedes albopictus",
         "Host Common Name": "mosquito", "Isolation Country": "India",
         "Geographic Location": "India", "Collection Year": 2019,
         "Other Names": None},
    ])

    store = db.DatabaseStore(
        violin_csv_paths={},  # load via dfs injection below
        bvbrc_csv_path=None,
        bvbrc_cache_dir=None,
        virus_resolution_cache_dir=None,
    )
    store.dfs["vaccines"] = vaccines_df
    store.dfs["pathogens"] = pathogens_df
    store.dfs["genes"] = genes_df
    store.dfs["vaccine_pathogen"] = vaccine_pathogen_df
    store.dfs["gene_vaccine_pathogen"] = gene_vaccine_pathogen_df
    store.dfs["bvbrc_genomes"] = bvbrc_df
    db.set_store_for_tests(store)
    return store


# ---------------------------------------------------------------------------
# No-data guard
# ---------------------------------------------------------------------------


def test_query_vaccines_no_data_returns_error_not_raise():
    """When data root is unset, tools return structured error — no exception."""
    import os
    old_root = os.environ.pop("APECX_DATA_ROOT", None)
    old_workspace = os.environ.pop("APECX_ROOT", None)
    try:
        result = asyncio.run(tools.query_vaccines())
        assert "error" in result
        assert isinstance(result["error"], str)
    finally:
        if old_root is not None:
            os.environ["APECX_DATA_ROOT"] = old_root
        if old_workspace is not None:
            os.environ["APECX_ROOT"] = old_workspace


# ---------------------------------------------------------------------------
# query_vaccines
# ---------------------------------------------------------------------------


def test_query_vaccines_no_filter_returns_all(small_store):
    out = asyncio.run(tools.query_vaccines())
    assert out["total_in_database"] == 3
    assert out["count"] == 3


def test_query_vaccines_search_term_filters(small_store):
    out = asyncio.run(tools.query_vaccines(search_term="EEEV"))
    assert out["total_matching"] == 1
    assert out["vaccines"][0]["Vaccine_Name"] == "Alpha Vaccine"


def test_query_vaccines_vaccine_type_filter(small_store):
    out = asyncio.run(tools.query_vaccines(vaccine_type="Subunit"))
    assert out["total_matching"] == 2


def test_query_vaccines_status_filter(small_store):
    out = asyncio.run(tools.query_vaccines(status="Licensed"))
    assert out["total_matching"] == 1


def test_query_vaccines_pathogen_filter_traverses_junction(small_store):
    out = asyncio.run(tools.query_vaccines(pathogen="Influenza"))
    assert out["total_matching"] == 1
    assert out["vaccines"][0]["Vaccine_Name"] == "Beta Vaccine"


def test_query_vaccines_limit_is_respected(small_store):
    out = asyncio.run(tools.query_vaccines(limit=1))
    assert out["count"] == 1
    assert out["total_matching"] == 3


# ---------------------------------------------------------------------------
# query_pathogens
# ---------------------------------------------------------------------------


def test_query_pathogens_all(small_store):
    out = asyncio.run(tools.query_pathogens())
    assert out["total_in_database"] == 2


def test_query_pathogens_disease_filter(small_store):
    out = asyncio.run(tools.query_pathogens(disease="Influenza"))
    assert out["total_matching"] == 1
    assert out["pathogens"][0]["Pathogen"] == "Influenza A virus"


def test_query_pathogens_enriches_vaccine_count(small_store):
    out = asyncio.run(tools.query_pathogens())
    recs = {r["Pathogen"]: r for r in out["pathogens"]}
    assert recs["Influenza A virus"]["vaccine_count"] == 1
    assert recs["Eastern equine encephalitis virus"]["vaccine_count"] == 1


# ---------------------------------------------------------------------------
# query_genes
# ---------------------------------------------------------------------------


def test_query_genes_all(small_store):
    out = asyncio.run(tools.query_genes())
    assert out["total_in_database"] == 2


def test_query_genes_organism_filter(small_store):
    out = asyncio.run(tools.query_genes(organism="Influenza"))
    assert out["total_matching"] == 1
    assert out["genes"][0]["Gene_Name"] == "HA"


def test_query_genes_search_term_protein_name(small_store):
    out = asyncio.run(tools.query_genes(search_term="Hemagglutinin"))
    assert out["total_matching"] == 1


# ---------------------------------------------------------------------------
# query_bvbrc_genomes
# ---------------------------------------------------------------------------


def test_query_bvbrc_all(small_store):
    out = asyncio.run(tools.query_bvbrc_genomes())
    assert out["total_in_database"] == 2


def test_query_bvbrc_species_filter(small_store):
    out = asyncio.run(tools.query_bvbrc_genomes(species="Chikungunya"))
    assert out["total_matching"] == 1
    assert out["genomes"][0]["Species"] == "Chikungunya virus"


def test_query_bvbrc_year_filter(small_store):
    out = asyncio.run(tools.query_bvbrc_genomes(min_year=2015))
    assert out["total_matching"] == 1
    assert out["genomes"][0]["Collection Year"] == 2019


def test_query_bvbrc_country_filter(small_store):
    out = asyncio.run(tools.query_bvbrc_genomes(country="India"))
    assert out["total_matching"] == 1


def test_query_bvbrc_zero_sentinel_skips_year_filter(small_store):
    # min_year=0 / max_year=0 must NOT filter — treated as "no filter"
    out = asyncio.run(tools.query_bvbrc_genomes(min_year=0, max_year=0))
    assert out["total_matching"] == 2


# ---------------------------------------------------------------------------
# get_vaccine_pathogen_genes
# ---------------------------------------------------------------------------


def test_get_vaccine_pathogen_genes_traversal(small_store):
    out = asyncio.run(tools.get_vaccine_pathogen_genes("Eastern equine encephalitis"))
    assert out["total_vaccines"] == 1
    assert out["total_genes"] == 1
    assert out["vaccines"][0]["vaccine_name"] == "Alpha Vaccine"
    assert out["vaccines"][0]["genes"][0]["gene_name"] == "E2"


def test_get_vaccine_pathogen_genes_unknown_pathogen_returns_empty(small_store):
    out = asyncio.run(tools.get_vaccine_pathogen_genes("Ebola"))
    assert out["total_vaccines"] == 0
    assert out["vaccines"] == []


# ---------------------------------------------------------------------------
# resolve_entity
# ---------------------------------------------------------------------------


def test_resolve_entity_matches_across_tables(small_store):
    out = asyncio.run(tools.resolve_entity("Influenza"))
    assert len(out["matches"]["pathogens"]) >= 1
    assert len(out["matches"]["vaccines"]) >= 1
    assert len(out["matches"]["genes"]) >= 1


def test_resolve_entity_returns_identifiers(small_store):
    out = asyncio.run(tools.resolve_entity("Eastern equine"))
    assert "11021" in out["identifiers"]["ncbi_taxonomy_ids"]


# ---------------------------------------------------------------------------
# database_statistics
# ---------------------------------------------------------------------------


def test_database_statistics_shows_all_loaded_tables(small_store):
    out = asyncio.run(tools.database_statistics())
    assert set(out["tables"].keys()) >= {
        "vaccines", "pathogens", "genes", "vaccine_pathogen",
        "gene_vaccine_pathogen", "bvbrc_genomes",
    }
    assert out["tables"]["vaccines"]["rows"] == 3
    assert "id" in out["tables"]["vaccines"]["columns"]
