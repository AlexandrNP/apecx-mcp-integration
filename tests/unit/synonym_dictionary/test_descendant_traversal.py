"""Unit tests for strict taxonomy hierarchy — descendant traversal.

Verifies that ``DictionaryIndex.lookup_descendant_taxon_ids`` correctly
expands a family-level IRI to all its descendant species/strains, and
that the ``query_pathogens`` data layer filters by the expanded set.

Strict hierarchy contract (user directive 2026-05-05):
  - "Coronaviridae" (family) → returns all coronavirus species/strains
  - "covid-19" / "SARS-CoV-2" (species) → returns ONLY SARS-CoV-2 entries

All tests use a synthetic hierarchy (no real NCBI taxdump required).
Integration tests that exercise the same code path on a real dictionary
with a real taxdump are in tests/integration/test_taxdump_real_hierarchy.py
(gated on APECX_NCBITAXON_TAXDUMP=1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

# ────────────────────────────────────────────────────────────────────────────
# Synthetic taxonomy tree used throughout:
#
#   11118 (Coronaviridae, family)
#       ├── 694002 (Betacoronavirus, genus)
#       │       ├── 2697049 (SARS-CoV-2, species)
#       │       └── 1335626 (MERS-CoV, species)
#       └── 693997 (Alphacoronavirus, genus)
#               └── 11120  (HCoV-229E, species)
# ────────────────────────────────────────────────────────────────────────────

_FAMILY_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_11118"
_GENUS_BETA_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_694002"
_SARS2_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_2697049"
_MERS_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_1335626"
_229E_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_11120"

# child_taxon_id → parent_taxon_id
_HIERARCHY = [
    (11118, 1),  # Coronaviridae → root
    (694002, 11118),  # Betacoronavirus → Coronaviridae
    (2697049, 694002),  # SARS-CoV-2 → Betacoronavirus
    (1335626, 694002),  # MERS-CoV → Betacoronavirus
    (693997, 11118),  # Alphacoronavirus → Coronaviridae
    (11120, 693997),  # HCoV-229E → Alphacoronavirus
]


def _make_test_db(tmp_path: Path) -> Path:
    """Build a minimal SQLite dictionary with hierarchy using the real writer."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "test_hierarchy.sqlite"

    manifest = BuildManifest(
        dictionary_version="test-1",
        built_at=datetime(2026, 1, 1, tzinfo=UTC),
        ontology_versions={OntologyName.NCBITAXON.value: "test"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 3},
        unresolved_count=0,
        record_count_total=3,
    )

    _built_at = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=_SARS2_IRI,
            canonical_label="Severe acute respiratory syndrome coronavirus 2",
            ontology=OntologyName.NCBITAXON,
            ontology_version="test",
            confidence=0.9,
            synonyms=["sars-cov-2", "covid-19", "2019-ncov"],
            source_records=("violin.pathogen.test.0",),
            resolved_at=_built_at,
        ),
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=_MERS_IRI,
            canonical_label="Middle East respiratory syndrome coronavirus",
            ontology=OntologyName.NCBITAXON,
            ontology_version="test",
            confidence=0.9,
            synonyms=["mers-cov"],
            source_records=("violin.pathogen.test.1",),
            resolved_at=_built_at,
        ),
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=_229E_IRI,
            canonical_label="Human coronavirus 229E",
            ontology=OntologyName.NCBITAXON,
            ontology_version="test",
            confidence=0.9,
            synonyms=["hcov-229e"],
            source_records=("violin.pathogen.test.2",),
            resolved_at=_built_at,
        ),
    ]

    with SQLiteDictionaryWriter(db) as writer:
        writer.write_manifest(manifest)
        for entry in entries:
            writer.write_entry(entry)
        writer.write_taxon_hierarchy(iter(_HIERARCHY))

    return db


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def idx(tmp_path_factory: pytest.TempPathFactory):
    """DictionaryIndex loaded from the synthetic hierarchy DB."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex

    db = _make_test_db(tmp_path_factory.mktemp("hierarchy"))
    return DictionaryIndex.load(db)


# ────────────────────────────────────────────────────────────────────────────
# Descendant traversal — unit tests
# ────────────────────────────────────────────────────────────────────────────


def test_family_iri_returns_all_descendant_ids(idx):
    """Coronaviridae IRI yields all species/genus IDs under it."""
    desc = idx.lookup_descendant_taxon_ids(_FAMILY_IRI)
    assert set(desc) >= {694002, 2697049, 1335626, 693997, 11120}, (
        "Expected all descendants of Coronaviridae family"
    )


def test_genus_iri_returns_only_its_species(idx):
    """Betacoronavirus (genus) yields only SARS-CoV-2 and MERS, not 229E."""
    desc = idx.lookup_descendant_taxon_ids(_GENUS_BETA_IRI)
    assert 2697049 in desc
    assert 1335626 in desc
    assert 11120 not in desc, "229E is an Alphacoronavirus, not under Betacoronavirus"


def test_species_iri_returns_empty_when_no_children(idx):
    """SARS-CoV-2 (leaf species) has no children in this synthetic tree."""
    desc = idx.lookup_descendant_taxon_ids(_SARS2_IRI)
    assert desc == [], f"SARS-CoV-2 should have no descendants, got {desc}"


def test_descendant_empty_without_hierarchy(tmp_path: Path):
    """Returns [] when dictionary was built without hierarchy."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.schema import BuildManifest
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "no_hier.sqlite"
    manifest = BuildManifest(
        dictionary_version="no-hier",
        built_at=datetime(2026, 1, 1, tzinfo=UTC),
        ontology_versions={},
        record_counts_per_entity_type={},
        unresolved_count=0,
        record_count_total=0,
    )
    with SQLiteDictionaryWriter(db) as writer:
        writer.write_manifest(manifest)
        # No write_taxon_hierarchy call — hierarchy table absent.

    no_hier_idx = DictionaryIndex.load(db)
    assert no_hier_idx.lookup_descendant_taxon_ids(_FAMILY_IRI) == []


def test_non_ncbitaxon_iri_returns_empty(idx):
    """Non-NCBITaxon IRI returns [] without error."""
    result = idx.lookup_descendant_taxon_ids("http://purl.obolibrary.org/obo/VO_0000001")
    assert result == []


# ────────────────────────────────────────────────────────────────────────────
# Strict hierarchy in database layer
# ────────────────────────────────────────────────────────────────────────────


def _make_store(pathogens_df):
    """Build a minimal DatabaseStore from a test DataFrame without reading CSVs."""

    from apecx_integration.mcp_surface.data.database import DatabaseStore

    store = object.__new__(DatabaseStore)
    store.dfs = {"pathogens": pathogens_df}
    store.virus_cache = {}
    return store


def test_query_pathogens_ncbi_taxonomy_ids_filters_by_set():
    """database.query_pathogens with ncbi_taxonomy_ids includes all IDs in set."""
    import pandas as pd

    from apecx_integration.mcp_surface.data.database import query_pathogens

    # Build minimal pathogen DataFrame with three species
    df = pd.DataFrame(
        [
            {"id": 1, "Pathogen": "SARS-CoV-2", "NCBI_Taxonomy_ID": "2697049"},
            {"id": 2, "Pathogen": "MERS-CoV", "NCBI_Taxonomy_ID": "1335626"},
            {"id": 3, "Pathogen": "Ebola", "NCBI_Taxonomy_ID": "186538"},
        ]
    )
    store = _make_store(df)

    # Query with Coronaviridae-expanded set → SARS-CoV-2 + MERS, not Ebola
    result = query_pathogens(store, ncbi_taxonomy_ids=[2697049, 1335626])
    pathogens = {r["Pathogen"] for r in result["pathogens"]}
    assert "SARS-CoV-2" in pathogens
    assert "MERS-CoV" in pathogens
    assert "Ebola" not in pathogens


def test_query_pathogens_ncbi_taxonomy_ids_takes_priority_over_single():
    """ncbi_taxonomy_ids takes priority over ncbi_taxonomy_id when both given."""
    import pandas as pd

    from apecx_integration.mcp_surface.data.database import query_pathogens

    df = pd.DataFrame(
        [
            {"id": 1, "Pathogen": "SARS-CoV-2", "NCBI_Taxonomy_ID": "2697049"},
            {"id": 2, "Pathogen": "MERS-CoV", "NCBI_Taxonomy_ID": "1335626"},
        ]
    )
    store = _make_store(df)

    # ncbi_taxonomy_ids=[2697049] → only SARS; ncbi_taxonomy_id=1335626 is ignored
    result = query_pathogens(
        store,
        ncbi_taxonomy_id=1335626,
        ncbi_taxonomy_ids=[2697049],
    )
    pathogens = {r["Pathogen"] for r in result["pathogens"]}
    assert pathogens == {"SARS-CoV-2"}


def test_query_pathogens_single_ncbi_id_still_works():
    """Legacy ncbi_taxonomy_id path still filters to exactly one taxon."""
    import pandas as pd

    from apecx_integration.mcp_surface.data.database import query_pathogens

    df = pd.DataFrame(
        [
            {"id": 1, "Pathogen": "SARS-CoV-2", "NCBI_Taxonomy_ID": "2697049"},
            {"id": 2, "Pathogen": "MERS-CoV", "NCBI_Taxonomy_ID": "1335626"},
        ]
    )
    store = _make_store(df)

    result = query_pathogens(store, ncbi_taxonomy_id=2697049)
    pathogens = {r["Pathogen"] for r in result["pathogens"]}
    assert pathogens == {"SARS-CoV-2"}


# ────────────────────────────────────────────────────────────────────────────
# Strict-hierarchy contract integration: family vs. species specificity
# ────────────────────────────────────────────────────────────────────────────


def test_family_query_wider_than_species_query(idx):
    """lookup_descendant_taxon_ids(family) is a strict superset of species."""
    family_ids = set(idx.lookup_descendant_taxon_ids(_FAMILY_IRI))
    sars2_ids = set(idx.lookup_descendant_taxon_ids(_SARS2_IRI))
    # SARS-CoV-2 is a leaf: no children
    assert sars2_ids == set(), "Species (leaf) should have no descendants"
    # Family has all species
    assert 2697049 in family_ids, "SARS-CoV-2 taxon ID must be under Coronaviridae"
    assert 1335626 in family_ids, "MERS-CoV must be under Coronaviridae"
    assert 11120 in family_ids, "HCoV-229E must be under Coronaviridae"
