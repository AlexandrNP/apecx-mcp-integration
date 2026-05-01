"""Round-trip tests for :class:`SQLiteDictionaryWriter` +
:class:`SQLiteDictionaryReader`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from apecx_integration.synonym_dictionary.enums import (
    EntityType,
    OntologyName,
)
from apecx_integration.synonym_dictionary.schema import (
    BuildManifest,
    DictionaryEntry,
)
from apecx_integration.synonym_dictionary.sqlite_writer import (
    SQLiteDictionaryReader,
    SQLiteDictionaryWriter,
)


def _sample_entry() -> DictionaryEntry:
    return DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        canonical_label="Chikungunya virus",
        synonyms=("CHIKV", "Chikungunya"),
        ontology=OntologyName.NCBITAXON,
        ontology_version="ncbitaxon-2026-04-01",
        source_records=("violin.pathogen.42",),
        confidence=1.0,
        resolved_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
    )


def _sample_manifest() -> BuildManifest:
    return BuildManifest(
        dictionary_version="2026-05-01.test",
        built_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        ontology_versions={"ncbitaxon": "2026-04-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
    )


def test_writer_creates_artifact_and_reader_round_trips(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "dictionary.sqlite"

    with SQLiteDictionaryWriter(artifact) as writer:
        writer.write_entry(_sample_entry())
        writer.write_manifest(_sample_manifest())

    assert artifact.exists()

    with SQLiteDictionaryReader(artifact) as reader:
        manifest = reader.read_manifest()
        assert manifest.dictionary_version == "2026-05-01.test"

        # Lookup by IRI.
        entry = reader.lookup_by_iri("http://purl.obolibrary.org/obo/NCBITaxon_37124")
        assert entry is not None
        assert entry.canonical_label == "Chikungunya virus"
        assert "CHIKV" in entry.synonyms

        # Lookup by surface form (one of the synonyms).
        entry2 = reader.lookup_by_surface_form(EntityType.PATHOGEN, "CHIKV")
        assert entry2 is not None
        assert entry2.canonical_iri == entry.canonical_iri

        # Lookup by surface form using mixed case + extra whitespace —
        # normalization MUST hit the same entry.
        entry3 = reader.lookup_by_surface_form(EntityType.PATHOGEN, "  chikungunya  ")
        assert entry3 is not None
        assert entry3.canonical_iri == entry.canonical_iri


def test_reader_raises_on_missing_manifest(tmp_path: Path) -> None:
    """A dictionary with entries but no manifest is a build that crashed
    mid-way.  Reader must refuse to open it rather than serve partial data."""
    artifact = tmp_path / "dictionary.sqlite"

    with SQLiteDictionaryWriter(artifact) as writer:
        writer.write_entry(_sample_entry())
        # Deliberately NOT writing the manifest.

    with pytest.raises(ValueError, match="manifest"):
        SQLiteDictionaryReader(artifact)


def test_reader_raises_on_incompatible_schema_major(tmp_path: Path) -> None:
    """Major-version bump support: reader refuses to open an artifact
    whose ``schema_version`` major is not in ``SUPPORTED_SCHEMA_MAJOR``."""
    artifact = tmp_path / "dictionary.sqlite"

    bumped_manifest = BuildManifest(
        schema_version="99.0.0",
        dictionary_version="future-build",
        built_at=datetime(2099, 1, 1, tzinfo=UTC),
        ontology_versions={},
        record_counts_per_entity_type={},
        unresolved_count=0,
        record_count_total=0,
    )

    with SQLiteDictionaryWriter(artifact) as writer:
        writer.write_entry(_sample_entry())
        writer.write_manifest(bumped_manifest)

    with pytest.raises(ValueError, match="not supported"):
        SQLiteDictionaryReader(artifact)


def test_writer_idempotent_on_repeat_write(tmp_path: Path) -> None:
    """Re-writing the same (entity_type, canonical_iri) replaces the
    previous entry rather than failing with a unique-key violation."""
    artifact = tmp_path / "dictionary.sqlite"

    with SQLiteDictionaryWriter(artifact) as writer:
        writer.write_entry(_sample_entry())
        # Same key, different label.
        replaced = _sample_entry().model_copy(update={"canonical_label": "Updated"})
        writer.write_entry(replaced)
        writer.write_manifest(_sample_manifest())

    with SQLiteDictionaryReader(artifact) as reader:
        entry = reader.lookup_by_iri("http://purl.obolibrary.org/obo/NCBITaxon_37124")
        assert entry is not None
        assert entry.canonical_label == "Updated"


def test_lookup_by_surface_form_returns_none_for_no_match(tmp_path: Path) -> None:
    artifact = tmp_path / "dictionary.sqlite"

    with SQLiteDictionaryWriter(artifact) as writer:
        writer.write_entry(_sample_entry())
        writer.write_manifest(_sample_manifest())

    with SQLiteDictionaryReader(artifact) as reader:
        assert reader.lookup_by_surface_form(EntityType.PATHOGEN, "nonsense") is None


def test_all_entries_iterates_every_entry(tmp_path: Path) -> None:
    artifact = tmp_path / "dictionary.sqlite"

    with SQLiteDictionaryWriter(artifact) as writer:
        writer.write_entry(_sample_entry())
        writer.write_entry(
            _sample_entry().model_copy(
                update={
                    "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_11021",
                    "canonical_label": "Eastern equine encephalitis virus",
                }
            )
        )
        writer.write_manifest(_sample_manifest())

    with SQLiteDictionaryReader(artifact) as reader:
        entries = list(reader.all_entries())
    assert len(entries) == 2
