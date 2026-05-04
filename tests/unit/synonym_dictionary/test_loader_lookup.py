"""Unit tests for Stage 2 loader and lookup (P3.10).

Tests are structured around the realistic boundary:
- DictionaryIndex.load() populates the in-memory hash from a real SQLite artifact
- lookup_entity() fast path hits the index
- lookup_entity() slow path is invoked when the index misses
- Process singleton: configure + get behave correctly

Integration tests (against a real built dictionary) belong in
tests/integration/test_stage2_lookup.py (gated on APECX_SYNONYM_DICT_LIVE_OLS=1
because they need a real built artifact).  These unit tests use a synthetic
SQLite written by the writer so they exercise the round-trip without OLS.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName, ResolutionStatus
from apecx_integration.synonym_dictionary.loader import DictionaryIndex, _ProcessSingleton
from apecx_integration.synonym_dictionary.lookup import LookupResult, lookup_entity
from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

# ---------------------------------------------------------------------------
# Helper: build a tiny SQLite for tests
# ---------------------------------------------------------------------------


def _write_test_dictionary(path: Path) -> None:
    """Write a minimal SQLite dictionary with three entries."""
    manifest = BuildManifest(
        dictionary_version="test-v1",
        built_at=datetime.now(UTC),
        ontology_versions={"ncbitaxon": "2026-04-01", "vo": "unknown"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 2, EntityType.VACCINE: 1},
        unresolved_count=0,
        record_count_total=3,
    )
    entries = [
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
            canonical_label="Chikungunya virus",
            synonyms=("CHIKV", "Chikungunya", "chikv"),
            ontology=OntologyName.NCBITAXON,
            ontology_version="2026-04-01",
            source_records=("violin.pathogen.1",),
            confidence=1.0,
            resolved_at=datetime.now(UTC),
        ),
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_11676",
            canonical_label="Human immunodeficiency virus 1",
            synonyms=("HIV-1", "HIV 1", "human immunodeficiency virus type 1"),
            ontology=OntologyName.NCBITAXON,
            ontology_version="2026-04-01",
            source_records=("violin.pathogen.2",),
            confidence=1.0,
            resolved_at=datetime.now(UTC),
        ),
        DictionaryEntry(
            entity_type=EntityType.VACCINE,
            canonical_iri="http://purl.obolibrary.org/obo/VO_0000122",
            canonical_label="influenza vaccine",
            synonyms=("flu shot", "influenza shot"),
            ontology=OntologyName.VO,
            ontology_version="unknown",
            source_records=("violin.vaccine.1",),
            confidence=0.9,
            resolved_at=datetime.now(UTC),
        ),
    ]
    with SQLiteDictionaryWriter(path) as writer:
        for entry in entries:
            writer.write_entry(entry)
        writer.write_manifest(manifest)


# ---------------------------------------------------------------------------
# DictionaryIndex tests
# ---------------------------------------------------------------------------


def test_load_populates_entries_and_index(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    assert index.entry_count() == 3
    assert index.index_entry_count() > 3  # canonical labels + synonyms


def test_lookup_exact_surface_form(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    entry = index.lookup(EntityType.PATHOGEN, "Chikungunya virus")
    assert entry is not None
    assert entry.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_37124"


def test_lookup_synonym_case_insensitive(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    # "CHIKV" is a synonym; lookup should find it regardless of case.
    entry = index.lookup(EntityType.PATHOGEN, "chikv")
    assert entry is not None
    assert "NCBITaxon_37124" in entry.canonical_iri

    entry2 = index.lookup(EntityType.PATHOGEN, "CHIKV")
    assert entry2 is not None
    assert entry2.canonical_iri == entry.canonical_iri


def test_lookup_miss_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    entry = index.lookup(EntityType.PATHOGEN, "definitely not a pathogen xyz")
    assert entry is None


def test_lookup_wrong_entity_type_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    # "influenza vaccine" is a VACCINE entry; looking it up as PATHOGEN should miss.
    entry = index.lookup(EntityType.PATHOGEN, "influenza vaccine")
    assert entry is None


def test_lookup_any_type_finds_across_types(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    # "flu shot" is a vaccine synonym — lookup_any_type should find it.
    results = index.lookup_any_type("flu shot")
    assert len(results) == 1
    assert results[0].entity_type == EntityType.VACCINE


def test_lookup_any_type_orders_by_confidence_desc(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    # HIV-1 has confidence 1.0; flu shot has 0.9.  A query for a term that
    # doesn't appear in both should still return in confidence order.
    results = index.lookup_any_type("hiv-1")
    assert results[0].confidence >= 1.0


# ---------------------------------------------------------------------------
# lookup_entity (top-level API) tests
# ---------------------------------------------------------------------------


def test_lookup_entity_fast_path_hit(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)

    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(db)

    result = lookup_entity("Chikungunya virus", entity_type=EntityType.PATHOGEN)
    assert isinstance(result, LookupResult)
    assert result.path == "fast"
    assert result.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    assert result.confidence == 1.0
    assert result.resolution_status == ResolutionStatus.ID_ANCHORED


def test_lookup_entity_returns_miss_on_no_dictionary_and_no_store(tmp_path: Path) -> None:
    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(tmp_path / "nonexistent.sqlite")

    result = lookup_entity("definitely not found xyz")
    assert isinstance(result, LookupResult)
    assert result.path == "miss"
    assert result.canonical_iri is None
    assert result.confidence == 0.0


def test_lookup_entity_empty_string_returns_miss() -> None:
    result = lookup_entity("")
    assert result.path == "miss"
    assert result.canonical_iri is None


def test_lookup_entity_whitespace_only_returns_miss() -> None:
    result = lookup_entity("   ")
    assert result.path == "miss"


def test_lookup_entity_synonym_lookup_no_entity_type(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)

    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(db)

    result = lookup_entity("HIV-1")
    assert result.path == "fast"
    assert "NCBITaxon_11676" in (result.canonical_iri or "")


# ---------------------------------------------------------------------------
# ProcessSingleton tests
# ---------------------------------------------------------------------------


def test_singleton_returns_error_when_path_not_set() -> None:
    s = _ProcessSingleton()
    idx, err = s.get()
    assert idx is None
    assert err is not None
    assert "APECX_SYNONYM_DICT_PATH" in err


def test_singleton_returns_error_when_path_missing(tmp_path: Path) -> None:
    s = _ProcessSingleton()
    s.configure(tmp_path / "missing.sqlite")
    idx, err = s.get()
    assert idx is None
    assert err is not None


def test_singleton_loads_index_from_valid_path(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    s = _ProcessSingleton()
    s.configure(db)
    idx, err = s.get()
    assert err is None
    assert isinstance(idx, DictionaryIndex)
    assert idx.entry_count() == 3


def test_singleton_reloads_on_path_change(tmp_path: Path) -> None:
    db1 = tmp_path / "dict1.sqlite"
    db2 = tmp_path / "dict2.sqlite"
    _write_test_dictionary(db1)
    _write_test_dictionary(db2)

    s = _ProcessSingleton()
    s.configure(db1)
    idx1, _ = s.get()

    s.configure(db2)
    idx2, _ = s.get()

    assert idx1 is not idx2


def test_singleton_thread_safe_concurrent_get(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    s = _ProcessSingleton()
    s.configure(db)

    results: list[DictionaryIndex | None] = []
    lock = threading.Lock()

    def get_and_record() -> None:
        idx, _ = s.get()
        with lock:
            results.append(idx)

    threads = [threading.Thread(target=get_and_record) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10
    assert all(r is not None for r in results)
    first = results[0]
    assert all(r is first for r in results), "singleton must return same instance"


# ---------------------------------------------------------------------------
# P3.4 — reverse IRI lookup (DictionaryIndex.lookup_by_iri + lookup_entity
#         IRI shortcut)
# ---------------------------------------------------------------------------


def test_lookup_by_iri_hit(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    entry = index.lookup_by_iri("http://purl.obolibrary.org/obo/NCBITaxon_37124")
    assert entry is not None
    assert entry.canonical_label == "Chikungunya virus"
    assert "CHIKV" in entry.synonyms


def test_lookup_by_iri_miss_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)

    assert index.lookup_by_iri("http://purl.obolibrary.org/obo/NCBITaxon_99999") is None


def test_lookup_entity_accepts_iri_as_surface_form(tmp_path: Path) -> None:
    """lookup_entity must fast-path on IRI input without surface-form normalization.

    Covered by: tests/integration/test_stage2_lookup.py::test_stage2_lookup_by_iri_real_data
    (gated on APECX_SYNONYM_DICT_LIVE_OLS=1).
    """
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)

    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(db)

    result = lookup_entity("http://purl.obolibrary.org/obo/NCBITaxon_37124")
    assert result.path == "fast"
    assert result.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    assert result.canonical_label == "Chikungunya virus"
    assert result.confidence == 1.0


def test_lookup_entity_iri_miss_falls_through(tmp_path: Path) -> None:
    """IRI miss in a dict WITHOUT hierarchy → falls through to slow/miss (no ancestor table)."""
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)  # no hierarchy table

    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(db)

    result = lookup_entity("http://purl.obolibrary.org/obo/NCBITaxon_99999")
    assert result.path in ("slow", "miss")
    assert result.canonical_iri is None


# ---------------------------------------------------------------------------
# P3.5 — ancestor traversal (DictionaryIndex.lookup_ancestor + lookup_entity
#         "ancestor" path)
# ---------------------------------------------------------------------------


def _write_dictionary_with_hierarchy(path: Path) -> None:
    """Write a SQLite dictionary that includes NCBITaxon_37124 (CHIKV, species-level)
    and a minimal hierarchy: NCBITaxon_99999 (synthetic strain) → 37124 → 11019 → 1.
    """
    manifest = BuildManifest(
        dictionary_version="test-v1",
        built_at=datetime.now(UTC),
        ontology_versions={"ncbitaxon": "2026-04-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
    )
    species_entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        canonical_label="Chikungunya virus",
        synonyms=("CHIKV",),
        ontology=OntologyName.NCBITAXON,
        ontology_version="2026-04-01",
        source_records=("violin.pathogen.1",),
        confidence=1.0,
        resolved_at=datetime.now(UTC),
    )
    hierarchy = [
        (99999, 37124),  # synthetic strain → CHIKV species
        (37124, 11019),  # CHIKV species → Alphavirus genus
        (11019, 1),  # Alphavirus → root
    ]
    with SQLiteDictionaryWriter(path) as writer:
        writer.write_entry(species_entry)
        writer.write_manifest(manifest)
        writer.write_taxon_hierarchy(iter(hierarchy))


def test_has_hierarchy_false_without_hierarchy_table(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)
    assert index.has_hierarchy is False


def test_has_hierarchy_true_when_table_written(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    _write_dictionary_with_hierarchy(db)
    index = DictionaryIndex.load(db)
    assert index.has_hierarchy is True


def test_lookup_ancestor_returns_none_no_hierarchy(tmp_path: Path) -> None:
    """lookup_ancestor returns None immediately when has_hierarchy is False."""
    db = tmp_path / "test.sqlite"
    _write_test_dictionary(db)
    index = DictionaryIndex.load(db)
    assert index.has_hierarchy is False
    result = index.lookup_ancestor("http://purl.obolibrary.org/obo/NCBITaxon_99999")
    assert result is None


def test_lookup_ancestor_returns_none_for_non_ncbitaxon_iri(tmp_path: Path) -> None:
    """lookup_ancestor only handles NCBITaxon IRIs; VO IRIs return None."""
    db = tmp_path / "test.sqlite"
    _write_dictionary_with_hierarchy(db)
    index = DictionaryIndex.load(db)
    result = index.lookup_ancestor("http://purl.obolibrary.org/obo/VO_0000122")
    assert result is None


def test_lookup_ancestor_walks_to_nearest_ancestor(tmp_path: Path) -> None:
    """Strain IRI not in dict → ancestor walk → species IRI in dict → return entry."""
    db = tmp_path / "test.sqlite"
    _write_dictionary_with_hierarchy(db)
    index = DictionaryIndex.load(db)

    # NCBITaxon_99999 is the synthetic strain — not in the dict directly
    entry = index.lookup_ancestor("http://purl.obolibrary.org/obo/NCBITaxon_99999")
    assert entry is not None
    assert entry.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    assert entry.canonical_label == "Chikungunya virus"


def test_lookup_ancestor_returns_none_when_no_ancestor_in_dict(tmp_path: Path) -> None:
    """If the ancestor walk finds no IRI that's in the dict, return None."""
    db = tmp_path / "test.sqlite"
    _write_dictionary_with_hierarchy(db)
    index = DictionaryIndex.load(db)

    # NCBITaxon_11019 (Alphavirus genus) is above CHIKV and NOT in the dict
    # Only CHIKV (37124) is in the dict, and 11019's parent chain leads to root (1)
    # 11019 itself has no parent in dict → None
    entry = index.lookup_ancestor("http://purl.obolibrary.org/obo/NCBITaxon_11019")
    # 11019 → parent 1 (root), root isn't in dict
    assert entry is None


def test_lookup_entity_takes_ancestor_path_for_strain_iri(tmp_path: Path) -> None:
    """lookup_entity returns path='ancestor' when strain IRI misses but ancestor found."""
    db = tmp_path / "test.sqlite"
    _write_dictionary_with_hierarchy(db)

    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(db)

    result = lookup_entity("http://purl.obolibrary.org/obo/NCBITaxon_99999")
    assert result.path == "ancestor"
    assert result.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    assert result.canonical_label == "Chikungunya virus"


def test_ancestor_result_confidence_is_penalized(tmp_path: Path) -> None:
    """Ancestor path confidence = ancestor_entry.confidence × 0.9."""
    db = tmp_path / "test.sqlite"
    _write_dictionary_with_hierarchy(db)

    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(db)

    result = lookup_entity("http://purl.obolibrary.org/obo/NCBITaxon_99999")
    assert result.path == "ancestor"
    # Species entry confidence is 1.0 → 1.0 * 0.9 = 0.9
    assert result.confidence == pytest.approx(0.9)


def test_ancestor_result_evidence_names_ancestor_iri(tmp_path: Path) -> None:
    """Ancestor path evidence string identifies the matched ancestor IRI."""
    db = tmp_path / "test.sqlite"
    _write_dictionary_with_hierarchy(db)

    from apecx_integration.synonym_dictionary import loader as _loader

    _loader._singleton.configure(db)

    result = lookup_entity("http://purl.obolibrary.org/obo/NCBITaxon_99999")
    assert "NCBITaxon_37124" in result.evidence
    assert "ancestor" in result.evidence.lower()


def test_lookup_ancestor_handles_merged_deprecated_taxon(tmp_path: Path) -> None:
    """A deprecated strain IRI (in merged.dmp) is remapped before ancestor walk."""
    db = tmp_path / "test.sqlite"
    manifest = BuildManifest(
        dictionary_version="test-v1",
        built_at=datetime.now(UTC),
        ontology_versions={"ncbitaxon": "2026-04-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
    )
    species_entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        canonical_label="Chikungunya virus",
        synonyms=("CHIKV",),
        ontology=OntologyName.NCBITAXON,
        ontology_version="2026-04-01",
        source_records=("violin.pathogen.1",),
        confidence=1.0,
        resolved_at=datetime.now(UTC),
    )
    with SQLiteDictionaryWriter(db) as writer:
        writer.write_entry(species_entry)
        writer.write_manifest(manifest)
        # 88888 → deprecated; merged to 99999 → parent 37124 (CHIKV species)
        writer.write_taxon_hierarchy(iter([(99999, 37124), (37124, 1)]))
        writer.write_merged_taxons(iter([(88888, 99999)]))

    index = DictionaryIndex.load(db)
    # Querying deprecated IRI 88888 should remap → 99999 → ancestor 37124
    entry = index.lookup_ancestor("http://purl.obolibrary.org/obo/NCBITaxon_88888")
    assert entry is not None
    assert entry.canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
