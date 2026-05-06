"""Adversarial probes batch 4 — probes 091-120.

Targets: lookup_entity() path routing, LookupResult invariants,
DictionaryIndex advanced methods (lookup_any_type multi-match ordering,
index_entry_count, load(), lookup_ambiguous_surface_forms filters),
and fast_miss/ancestor-result confidence arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(tz=UTC)


def _make_entry(
    *,
    entity_type=None,
    label: str = "Test Pathogen",
    iri: str = "http://purl.obolibrary.org/obo/NCBITaxon_99999",
    synonyms: tuple = (),
    confidence: float = 1.0,
):
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    return DictionaryEntry(
        entity_type=entity_type if entity_type is not None else EntityType.PATHOGEN,
        canonical_iri=iri,
        canonical_label=label,
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=confidence,
        resolved_at=_now(),
        source_records=("test",),
        synonyms=synonyms,
    )


def _make_manifest(entity_type=None, count: int = 1):
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    et = entity_type if entity_type is not None else EntityType.PATHOGEN
    return BuildManifest(
        dictionary_version="test-batch4",
        built_at=_now(),
        ontology_versions={"ncbitaxon": "2024-01-01"},
        record_counts_per_entity_type={et: count},
        unresolved_count=0,
        record_count_total=count,
    )


def _build_index(entries, *, db_path=None):
    """Build an in-memory DictionaryIndex from a list of entries."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    manifest = _make_manifest(count=len(entries))
    inverse: dict[tuple[str, str], str] = {}
    entries_dict: dict[str, object] = {}
    for entry in entries:
        entries_dict[entry.canonical_iri] = entry
        for surface in (entry.canonical_label, *entry.synonyms):
            normalized = normalize_surface_form(surface)
            if normalized:
                key = (entry.entity_type.value, normalized)
                if key not in inverse:
                    inverse[key] = entry.canonical_iri
    return DictionaryIndex(
        inverse=inverse,
        entries=entries_dict,
        manifest=manifest,
        db_path=db_path,
        has_hierarchy=False,
    )


# ---------------------------------------------------------------------------
# lookup_entity() path routing (091-100)
# ---------------------------------------------------------------------------


def test_probe_091_lookup_entity_empty_string_returns_miss():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    result = lookup_entity("")
    assert result.path == "miss"


def test_probe_092_lookup_entity_whitespace_only_returns_miss():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    result = lookup_entity("   ")
    assert result.path == "miss"


def test_probe_093_lookup_entity_miss_has_zero_confidence():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    result = lookup_entity("xyzzy_nonexistent_entity_12345678")
    assert result.confidence == 0.0


def test_probe_094_lookup_entity_miss_has_none_iri():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    result = lookup_entity("xyzzy_nonexistent_entity_12345678")
    assert result.canonical_iri is None


def test_probe_095_lookup_entity_miss_resolution_status_unresolved():
    from apecx_integration.synonym_dictionary.enums import ResolutionStatus
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    result = lookup_entity("xyzzy_nonexistent_entity_12345678")
    assert result.resolution_status == ResolutionStatus.UNRESOLVED


def test_probe_096_fast_miss_path_field_is_miss():
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("foo")
    assert result.path == "miss"


def test_probe_097_fast_miss_confidence_is_zero():
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("foo", reason="test")
    assert result.confidence == 0.0


def test_probe_098_fast_miss_evidence_contains_reason():
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("foo", reason="my test reason")
    assert "my test reason" in result.evidence


def test_probe_099_fast_miss_surface_form_preserved():
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("SomePathogen")
    assert result.surface_form == "SomePathogen"


def test_probe_100_lookup_result_is_frozen():
    """LookupResult is a frozen dataclass — attribute assignment must raise."""
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("test")
    with pytest.raises((AttributeError, TypeError)):
        result.confidence = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DictionaryIndex advanced methods (101-110)
# ---------------------------------------------------------------------------


def test_probe_101_lookup_any_type_multi_entity_returns_sorted_by_confidence():
    """lookup_any_type with two entries at different confidences returns highest first."""
    from apecx_integration.synonym_dictionary.enums import EntityType

    e_low = _make_entry(
        entity_type=EntityType.PATHOGEN,
        label="flu",
        iri="http://purl.obolibrary.org/obo/NCBITaxon_11520",
        confidence=0.5,
    )
    e_high = _make_entry(
        entity_type=EntityType.DISEASE,
        label="flu",
        iri="http://purl.obolibrary.org/obo/DOID_8469",
        confidence=0.9,
    )
    idx = _build_index([e_low, e_high])
    results = idx.lookup_any_type("flu")
    assert len(results) >= 2
    assert results[0].confidence >= results[1].confidence


def test_probe_102_lookup_any_type_deduplicates_by_iri():
    """When the same IRI is reachable via two surface forms, it appears once."""
    from apecx_integration.synonym_dictionary.enums import EntityType

    # Two synonyms for the same IRI
    e = _make_entry(
        entity_type=EntityType.PATHOGEN,
        label="SARS-CoV-2",
        iri="http://purl.obolibrary.org/obo/NCBITaxon_2697049",
        synonyms=("COVID-19 virus",),
    )
    idx = _build_index([e])
    results = idx.lookup_any_type("sars-cov-2")
    iris = [r.canonical_iri for r in results]
    assert len(iris) == len(set(iris)), "IRI appeared more than once"


def test_probe_103_index_entry_count_matches_entries():
    entries = [
        _make_entry(label="PathA", iri="http://example.org/1"),
        _make_entry(label="PathB", iri="http://example.org/2"),
        _make_entry(label="PathC", iri="http://example.org/3"),
    ]
    idx = _build_index(entries)
    assert idx.entry_count() == 3


def test_probe_104_index_entry_count_increases_with_synonyms():
    """index_entry_count (inverse index) > entry_count when synonyms exist."""
    entry = _make_entry(
        label="Ebola virus",
        iri="http://purl.obolibrary.org/obo/NCBITaxon_186538",
        synonyms=("EBOV", "Sudan virus"),
    )
    idx = _build_index([entry])
    # 1 canonical label + 2 synonyms = 3 index rows, 1 entry
    assert idx.index_entry_count() >= 3
    assert idx.entry_count() == 1


def test_probe_105_lookup_ambiguous_surface_forms_no_db_returns_empty():
    """Without a db_path, lookup_ambiguous_surface_forms returns [] not an error."""
    idx = _build_index([_make_entry()])
    result = idx.lookup_ambiguous_surface_forms()
    assert result == []


def test_probe_106_lookup_ambiguous_surface_forms_with_db_no_conflicts_returns_empty(
    tmp_path,
):
    """After a write with no conflicts, lookup_ambiguous_surface_forms returns []."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    manifest = _make_manifest()
    entry = _make_entry()
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(manifest)
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    result = idx.lookup_ambiguous_surface_forms()
    assert result == []


def test_probe_107_lookup_ambiguous_surface_forms_surface_form_filter(tmp_path):
    """Filter by surface_form returns only matching conflict rows.

    Conflict triggered by writing two entries with the same normalized label
    but different canonical IRIs — write_entry records this automatically.
    """
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    manifest = _make_manifest(count=3)

    # "flu" conflict: two pathogen entries share the label "flu" (different IRIs)
    e_flu_1 = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/flu1",
        canonical_label="flu",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.9,
        resolved_at=_now(),
    )
    e_flu_2 = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/flu2",
        canonical_label="flu",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.8,
        resolved_at=_now(),
    )
    # "dengue" conflict: also two entries
    e_dengue_1 = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/dengue1",
        canonical_label="dengue",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.9,
        resolved_at=_now(),
    )
    e_dengue_2 = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/dengue2",
        canonical_label="dengue",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.8,
        resolved_at=_now(),
    )
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(manifest)
        w.write_entry(e_flu_1)
        w.write_entry(e_flu_2)  # triggers "flu" conflict
        w.write_entry(e_dengue_1)
        w.write_entry(e_dengue_2)  # triggers "dengue" conflict

    idx = DictionaryIndex.load(db)
    results = idx.lookup_ambiguous_surface_forms(surface_form="flu")
    assert len(results) >= 1
    assert all(r["surface_form_normalized"] == "flu" for r in results)


def test_probe_108_lookup_ambiguous_surface_forms_entity_type_filter(tmp_path):
    """Filter by entity_type returns only matching conflict rows.

    Two entity types (pathogen and disease) each have a "fever" conflict.
    The entity_type filter should restrict to only one type.
    """
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    manifest = _make_manifest(count=4)

    def _make_disease(iri: str) -> DictionaryEntry:
        return DictionaryEntry(
            entity_type=EntityType.DISEASE,
            canonical_iri=iri,
            canonical_label="fever",
            ontology=OntologyName.DOID,
            ontology_version="2024-01-01",
            confidence=0.9,
            resolved_at=_now(),
        )

    def _make_pathogen(iri: str) -> DictionaryEntry:
        return DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=iri,
            canonical_label="fever",
            ontology=OntologyName.NCBITAXON,
            ontology_version="2024-01-01",
            confidence=0.9,
            resolved_at=_now(),
        )

    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(manifest)
        w.write_entry(_make_pathogen("http://example.org/p1"))
        w.write_entry(_make_pathogen("http://example.org/p2"))  # pathogen "fever" conflict
        w.write_entry(_make_disease("http://example.org/d1"))
        w.write_entry(_make_disease("http://example.org/d2"))  # disease "fever" conflict

    idx = DictionaryIndex.load(db)
    results = idx.lookup_ambiguous_surface_forms(entity_type="disease")
    assert len(results) >= 1
    assert all(r["entity_type"] == "disease" for r in results)


def test_probe_109_lookup_ancestor_no_hierarchy_returns_none():
    """Without has_hierarchy, lookup_ancestor always returns None."""
    idx = _build_index([_make_entry()])
    result = idx.lookup_ancestor("http://purl.obolibrary.org/obo/NCBITaxon_99999")
    assert result is None


def test_probe_110_lookup_ancestor_non_ncbitaxon_iri_returns_none():
    """lookup_ancestor only handles NCBITaxon IRIs; others return None immediately."""
    idx = _build_index([_make_entry()])
    # Would need has_hierarchy=True to even attempt; but with non-NCBITaxon IRI
    # it returns None regardless of hierarchy state
    result = idx.lookup_ancestor("http://purl.obolibrary.org/obo/DOID_526")
    assert result is None


# ---------------------------------------------------------------------------
# DictionaryIndex.load() and LookupResult contracts (111-120)
# ---------------------------------------------------------------------------


def test_probe_111_dictionary_index_load_roundtrip(tmp_path):
    """DictionaryIndex.load() finds entries written by SQLiteDictionaryWriter."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    entry = _make_entry(label="West Nile Virus", synonyms=("WNV",))
    manifest = _make_manifest()
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(manifest)
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    found = idx.lookup(EntityType.PATHOGEN, "West Nile Virus")
    assert found is not None
    assert found.canonical_iri == entry.canonical_iri


def test_probe_112_dictionary_index_load_synonym_lookup(tmp_path):
    """After load, looking up a synonym also resolves to the canonical entry."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    entry = _make_entry(label="Zika Virus", synonyms=("ZIKV", "Zika fever virus"))
    manifest = _make_manifest()
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(manifest)
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    found = idx.lookup(EntityType.PATHOGEN, "ZIKV")
    assert found is not None
    assert found.canonical_iri == entry.canonical_iri


def test_probe_113_dictionary_index_load_preserves_manifest(tmp_path):
    """DictionaryIndex.load() exposes the manifest from the SQLite file."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    manifest = _make_manifest()
    entry = _make_entry()
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(manifest)
        w.write_entry(entry)

    idx = DictionaryIndex.load(db)
    assert idx.manifest.dictionary_version == manifest.dictionary_version


def test_probe_114_lookup_result_synonyms_default_empty_tuple():
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("query")
    assert result.synonyms == ()


def test_probe_115_lookup_result_evidence_default_empty_string():
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    # When no reason given
    result = fast_miss("query")
    assert isinstance(result.evidence, str)


def test_probe_116_lookup_result_path_field_one_of_four_literals():
    """path must be one of 'fast', 'ancestor', 'slow', 'miss'."""
    from apecx_integration.synonym_dictionary.lookup import fast_miss

    result = fast_miss("query")
    assert result.path in {"fast", "ancestor", "slow", "miss"}


def test_probe_117_entry_to_result_confidence_1_sets_id_anchored():
    """_entry_to_result sets resolution_status=ID_ANCHORED for confidence=1.0."""
    from apecx_integration.synonym_dictionary.enums import ResolutionStatus
    from apecx_integration.synonym_dictionary.lookup import _entry_to_result

    entry = _make_entry(confidence=1.0)
    result = _entry_to_result("test", entry, path="fast")
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED


def test_probe_118_entry_to_result_confidence_below_1_sets_ols_fuzzy():
    """_entry_to_result sets resolution_status=OLS_FUZZY for confidence < 1.0."""
    from apecx_integration.synonym_dictionary.enums import ResolutionStatus
    from apecx_integration.synonym_dictionary.lookup import _entry_to_result

    entry = _make_entry(confidence=0.7)
    result = _entry_to_result("test", entry, path="fast")
    assert result.resolution_status == ResolutionStatus.OLS_FUZZY


def test_probe_119_entry_to_result_preserves_iri():
    from apecx_integration.synonym_dictionary.lookup import _entry_to_result

    entry = _make_entry(iri="http://purl.obolibrary.org/obo/NCBITaxon_11234")
    result = _entry_to_result("test", entry, path="fast")
    assert result.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_11234"


def test_probe_120_ancestor_result_confidence_is_parent_times_0_9():
    """_ancestor_to_result multiplies confidence by 0.9 and rounds to 4 places."""
    from apecx_integration.synonym_dictionary.lookup import _ancestor_to_result

    ancestor = _make_entry(confidence=1.0)
    result = _ancestor_to_result("http://purl.obolibrary.org/obo/NCBITaxon_12345", ancestor)
    assert result.path == "ancestor"
    assert result.confidence == round(1.0 * 0.9, 4)
