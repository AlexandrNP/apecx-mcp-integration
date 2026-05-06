"""Adversarial probes batch 9 — probes 241-270.

Targets: ResolutionResult schema (extra='forbid', field types, confidence
range), ProvisionalSynonym schema, BuildManifest schema_version defaults,
SQLiteDictionaryWriter merged taxons / taxon hierarchy counts, and
SQLiteDictionaryReader schema-version roundtrip.
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


def _make_manifest(version: str = "test-batch9"):
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    return BuildManifest(
        dictionary_version=version,
        built_at=_now(),
        ontology_versions={"ncbitaxon": "2024-01-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 0},
        unresolved_count=0,
        record_count_total=0,
    )


# ---------------------------------------------------------------------------
# ResolutionResult schema (241-250)
# ---------------------------------------------------------------------------


def test_probe_241_resolution_result_extra_field_forbidden():
    from apecx_integration.synonym_dictionary.enums import OntologyName, ResolutionStatus
    from apecx_integration.synonym_dictionary.schema import ResolutionResult

    with pytest.raises(Exception):  # noqa: B017
        ResolutionResult(
            canonical_iri="http://example.org/1",
            canonical_label="Test",
            canonical_ontology=OntologyName.NCBITAXON,
            resolution_status=ResolutionStatus.ID_ANCHORED,
            resolution_confidence=1.0,
            dictionary_version="1.0",
            extra_field="bad",  # extra='forbid'
        )


def test_probe_242_resolution_result_confidence_must_be_0_to_1():
    """resolution_confidence outside [0,1] is rejected."""
    from apecx_integration.synonym_dictionary.enums import ResolutionStatus
    from apecx_integration.synonym_dictionary.schema import ResolutionResult

    with pytest.raises(Exception):  # noqa: B017
        ResolutionResult(
            canonical_iri=None,
            canonical_label=None,
            canonical_ontology=None,
            resolution_status=ResolutionStatus.UNRESOLVED,
            resolution_confidence=1.5,  # > 1.0
            dictionary_version="1.0",
        )


def test_probe_243_resolution_result_confidence_negative_rejected():
    from apecx_integration.synonym_dictionary.enums import ResolutionStatus
    from apecx_integration.synonym_dictionary.schema import ResolutionResult

    with pytest.raises(Exception):  # noqa: B017
        ResolutionResult(
            canonical_iri=None,
            canonical_label=None,
            canonical_ontology=None,
            resolution_status=ResolutionStatus.UNRESOLVED,
            resolution_confidence=-0.1,
            dictionary_version="1.0",
        )


def test_probe_244_resolution_result_synonyms_default_empty_tuple():
    from apecx_integration.synonym_dictionary.enums import OntologyName, ResolutionStatus
    from apecx_integration.synonym_dictionary.schema import ResolutionResult

    r = ResolutionResult(
        canonical_iri="http://example.org/1",
        canonical_label="Test",
        canonical_ontology=OntologyName.NCBITAXON,
        resolution_status=ResolutionStatus.ID_ANCHORED,
        resolution_confidence=1.0,
        dictionary_version="1.0",
    )
    assert r.synonyms == ()


def test_probe_245_resolution_result_unresolved_accepts_none_iri():
    from apecx_integration.synonym_dictionary.enums import ResolutionStatus
    from apecx_integration.synonym_dictionary.schema import ResolutionResult

    r = ResolutionResult(
        canonical_iri=None,
        canonical_label=None,
        canonical_ontology=None,
        resolution_status=ResolutionStatus.UNRESOLVED,
        resolution_confidence=0.0,
        dictionary_version="1.0",
    )
    assert r.canonical_iri is None


# ---------------------------------------------------------------------------
# ProvisionalSynonym schema (246-250)
# ---------------------------------------------------------------------------


def test_probe_246_provisional_synonym_extra_field_forbidden():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import ProvisionalSynonym

    with pytest.raises(Exception):  # noqa: B017
        ProvisionalSynonym(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=None,
            surface_form="EEEV",
            proposed_at=_now(),
            proposed_by="alice",
            confidence=0.8,
            garbage_field="bad",
        )


def test_probe_247_provisional_synonym_confidence_range_enforced():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import ProvisionalSynonym

    with pytest.raises(Exception):  # noqa: B017
        ProvisionalSynonym(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=None,
            surface_form="EEEV",
            proposed_at=_now(),
            proposed_by="alice",
            confidence=2.5,
        )


def test_probe_248_provisional_synonym_nil_iri_allowed():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import ProvisionalSynonym

    ps = ProvisionalSynonym(
        entity_type=EntityType.PATHOGEN,
        canonical_iri=None,
        surface_form="unknown agent",
        proposed_at=_now(),
        proposed_by="anonymous",
        confidence=0.0,
    )
    assert ps.canonical_iri is None


def test_probe_249_provisional_synonym_promotion_signals_default_empty():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import ProvisionalSynonym

    ps = ProvisionalSynonym(
        entity_type=EntityType.VACCINE,
        canonical_iri="http://example.org/v1",
        surface_form="mRNA shot",
        proposed_at=_now(),
        proposed_by="user123",
        confidence=0.6,
    )
    assert ps.promotion_signals == ()


def test_probe_250_provisional_synonym_promotion_signals_is_tuple():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import ProvisionalSynonym

    ps = ProvisionalSynonym(
        entity_type=EntityType.VACCINE,
        canonical_iri="http://example.org/v1",
        surface_form="mRNA shot",
        proposed_at=_now(),
        proposed_by="user123",
        confidence=0.6,
        promotion_signals=("vote:alice", "vote:bob"),
    )
    assert isinstance(ps.promotion_signals, tuple)


# ---------------------------------------------------------------------------
# BuildManifest schema_version default + harvester_version optional (251-255)
# ---------------------------------------------------------------------------


def test_probe_251_build_manifest_schema_version_default_1_0_0():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    m = BuildManifest(
        dictionary_version="test",
        built_at=_now(),
        ontology_versions={"ncbitaxon": "2024"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 0},
        unresolved_count=0,
        record_count_total=0,
    )
    assert m.schema_version == "1.0.0"


def test_probe_252_build_manifest_harvester_version_default_none():
    m = _make_manifest()
    assert m.harvester_version is None


def test_probe_253_build_manifest_harvester_version_can_be_set():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    m = BuildManifest(
        dictionary_version="v2",
        built_at=_now(),
        ontology_versions={"ncbitaxon": "2024"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
        harvester_version="apecx-harvesters-1.2.3",
    )
    assert m.harvester_version == "apecx-harvesters-1.2.3"


def test_probe_254_build_manifest_unresolved_count_nonnegative():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    with pytest.raises(Exception):  # noqa: B017
        BuildManifest(
            dictionary_version="v1",
            built_at=_now(),
            ontology_versions={},
            record_counts_per_entity_type={EntityType.PATHOGEN: 0},
            unresolved_count=-1,
            record_count_total=0,
        )


def test_probe_255_build_manifest_record_count_total_nonnegative():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    with pytest.raises(Exception):  # noqa: B017
        BuildManifest(
            dictionary_version="v1",
            built_at=_now(),
            ontology_versions={},
            record_counts_per_entity_type={EntityType.PATHOGEN: 0},
            unresolved_count=0,
            record_count_total=-5,
        )


# ---------------------------------------------------------------------------
# SQLiteDictionaryWriter taxon / merged taxon operations (256-260)
# ---------------------------------------------------------------------------


def test_probe_256_write_taxon_hierarchy_returns_row_count(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        count = w.write_taxon_hierarchy(iter([(9606, 1), (9605, 9604)]))
    assert count == 2


def test_probe_257_has_taxon_hierarchy_after_write(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_taxon_hierarchy(iter([(9606, 1)]))
    # Re-read
    with SQLiteDictionaryWriter(db) as w:
        assert w.has_taxon_hierarchy()


def test_probe_258_has_taxon_hierarchy_false_without_write(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        assert not w.has_taxon_hierarchy()


def test_probe_259_write_merged_taxons_returns_count(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        count = w.write_merged_taxons(iter([(11234, 9999)]))
    assert count == 1


def test_probe_260_schema_version_roundtrips_through_sqlite(tmp_path):
    """The manifest's schema_version survives a write → read cycle."""
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    manifest = _make_manifest()
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(manifest)

    reader = SQLiteDictionaryReader(db)
    loaded = reader.read_manifest()
    assert loaded.schema_version == manifest.schema_version


# ---------------------------------------------------------------------------
# SQLiteDictionaryReader edge cases (261-270)
# ---------------------------------------------------------------------------


def test_probe_261_reader_all_entries_returns_iterator(tmp_path):
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/batch9",
        canonical_label="Batch9 Pathogen",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=1.0,
        resolved_at=_now(),
    )
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    reader = SQLiteDictionaryReader(db)
    entries = list(reader.all_entries())
    assert len(entries) == 1
    assert entries[0].canonical_iri == entry.canonical_iri


def test_probe_262_reader_all_entries_empty_db_yields_nothing(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())

    reader = SQLiteDictionaryReader(db)
    assert list(reader.all_entries()) == []


def test_probe_263_reader_has_taxon_hierarchy_false_for_no_hierarchy(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())

    reader = SQLiteDictionaryReader(db)
    assert not reader.has_taxon_hierarchy()


def test_probe_264_reader_has_taxon_hierarchy_true_after_write(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_taxon_hierarchy(iter([(9606, 1)]))

    reader = SQLiteDictionaryReader(db)
    assert reader.has_taxon_hierarchy()


def test_probe_265_reader_manifest_dictionary_version_roundtrip(tmp_path):
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest(version="roundtrip-test-v3"))

    reader = SQLiteDictionaryReader(db)
    m = reader.read_manifest()
    assert m.dictionary_version == "roundtrip-test-v3"


def test_probe_266_reader_entry_source_records_roundtrip(tmp_path):
    """source_records tuple survives write → read."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/srtest",
        canonical_label="SR Test",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.9,
        resolved_at=_now(),
        source_records=("violin.pathogen.42", "bvbrc.genome.99"),
    )
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    reader = SQLiteDictionaryReader(db)
    loaded = list(reader.all_entries())[0]
    assert set(loaded.source_records) == {"violin.pathogen.42", "bvbrc.genome.99"}


def test_probe_267_reader_entry_synonyms_roundtrip(tmp_path):
    """synonyms tuple survives write → read."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/syntest",
        canonical_label="Syn Test",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.9,
        resolved_at=_now(),
        synonyms=("SynA", "SynB", "SynC"),
    )
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    reader = SQLiteDictionaryReader(db)
    loaded = list(reader.all_entries())[0]
    assert set(loaded.synonyms) == {"SynA", "SynB", "SynC"}


def test_probe_268_reader_entry_confidence_roundtrip(tmp_path):
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://example.org/conftest",
        canonical_label="Conf Test",
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.73,
        resolved_at=_now(),
    )
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    reader = SQLiteDictionaryReader(db)
    loaded = list(reader.all_entries())[0]
    assert abs(loaded.confidence - 0.73) < 1e-9


def test_probe_269_reader_entry_entity_type_roundtrip(tmp_path):
    """entity_type enum value survives write → read as the same enum member."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    entry = DictionaryEntry(
        entity_type=EntityType.VACCINE,
        canonical_iri="http://example.org/vactest",
        canonical_label="Vac Test",
        ontology=OntologyName.VO,
        ontology_version="2024-01-01",
        confidence=1.0,
        resolved_at=_now(),
    )
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        w.write_entry(entry)

    reader = SQLiteDictionaryReader(db)
    loaded = list(reader.all_entries())[0]
    assert loaded.entity_type == EntityType.VACCINE


def test_probe_270_reader_multiple_entries_all_returned(tmp_path):
    """With N entries written, all_entries yields all N."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    db = tmp_path / "dict.sqlite"
    entries = [
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=f"http://example.org/{i}",
            canonical_label=f"Pathogen {i}",
            ontology=OntologyName.NCBITAXON,
            ontology_version="2024-01-01",
            confidence=1.0,
            resolved_at=_now(),
        )
        for i in range(5)
    ]
    with SQLiteDictionaryWriter(db) as w:
        w.write_manifest(_make_manifest())
        for e in entries:
            w.write_entry(e)

    reader = SQLiteDictionaryReader(db)
    loaded = list(reader.all_entries())
    assert len(loaded) == 5
