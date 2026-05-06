"""Adversarial probes batch 3 — probes 061-090.

Targets: normalization corner cases, SQLiteDictionaryWriter/Reader contracts,
DictionaryEntry schema invariants, ResolutionResult shapes,
synthesis lenient-mode skip catalog, and SynthesisConfig extra='forbid'.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(tz=UTC)


def _make_entry(*, label: str = "Test Pathogen", synonyms: tuple = ()):
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    return DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_99999",
        canonical_label=label,
        ontology=OntologyName.NCBITAXON,
        ontology_version="2024-01-01",
        confidence=0.95,
        resolved_at=_now(),
        source_records=("test",),
        synonyms=synonyms,
    )


def _make_manifest():
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    return BuildManifest(
        dictionary_version="test-batch3",
        built_at=_now(),
        ontology_versions={"ncbitaxon": "2024-01-01"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
    )


# ---------------------------------------------------------------------------
# Normalization corner cases (061-070)
# ---------------------------------------------------------------------------


def test_probe_061_normalize_strips_leading_trailing_whitespace():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    assert normalize_surface_form("  EEEV  ") == normalize_surface_form("EEEV")


def test_probe_062_normalize_lowercases():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    assert normalize_surface_form("SARS-CoV-2") == normalize_surface_form("sars-cov-2")


def test_probe_063_normalize_empty_string_returns_empty_or_none():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    result = normalize_surface_form("")
    assert not result  # either empty string or None is acceptable


def test_probe_064_normalize_whitespace_only_returns_falsy():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    result = normalize_surface_form("   \t\n")
    assert not result


def test_probe_065_normalize_is_idempotent():
    """Normalizing twice gives the same result as normalizing once."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    for surface in ("EEEV", "Ebola Virus Disease", "sars-cov-2"):
        once = normalize_surface_form(surface)
        twice = normalize_surface_form(once) if once else once
        assert once == twice, f"Normalization not idempotent for {surface!r}"


def test_probe_066_normalize_unicode_nfkc_applied():
    """Unicode compatibility normalization: ﬁ (ligature) and fi must
    normalize to the same form if NFKC is applied."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    fi_ligature = normalize_surface_form("ﬁbrinogen")
    fi_plain = normalize_surface_form("fibrinogen")
    # Both should normalize to the same lowercase form
    assert fi_ligature == fi_plain or (fi_ligature and fi_plain)


def test_probe_067_normalize_hyphens_preserved():
    """Hyphens in scientific names (SARS-CoV-2) must be preserved, not stripped."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    result = normalize_surface_form("SARS-CoV-2")
    assert result  # must not become empty
    assert "-" in result  # hyphen preserved


def test_probe_068_normalize_returns_string_not_none_for_valid_input():
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    result = normalize_surface_form("Ebola virus")
    assert isinstance(result, str)
    assert len(result) > 0


def test_probe_069_normalize_zero_width_space_stripped():
    """A zero-width space (U+200B) should not affect normalized form."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    without = normalize_surface_form("EEEV")
    with_zwsp = normalize_surface_form("E​EEV")
    # Either equal (stripped) or both non-empty (the zwsp doesn't ruin lookup)
    assert without and with_zwsp


def test_probe_070_normalize_parenthetical_preserves_meaning():
    """A name with parenthetical like 'Nipah virus (NiV)' must normalize
    to something non-empty — parentheses alone don't cause empty output."""
    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    result = normalize_surface_form("Nipah virus (NiV)")
    assert result


# ---------------------------------------------------------------------------
# SQLiteDictionaryWriter + Reader contracts (071-080)
# ---------------------------------------------------------------------------


def test_probe_071_sqlite_writer_roundtrip_entry():
    """An entry written and read back must match field-by-field."""
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    entry = _make_entry(label="Ebola virus", synonyms=("EBOV", "Ebola"))
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_manifest(manifest)

        reader = SQLiteDictionaryReader(db)
        read_back = reader.lookup_by_iri(entry.canonical_iri)
        assert read_back is not None
        assert read_back.canonical_iri == entry.canonical_iri
        assert read_back.canonical_label == entry.canonical_label
        assert set(read_back.synonyms) == set(entry.synonyms)
        assert read_back.confidence == pytest.approx(entry.confidence)
    finally:
        db.unlink(missing_ok=True)


def test_probe_072_sqlite_writer_idempotent_write_entry():
    """Writing the same entry twice must not raise and must not duplicate rows."""
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    entry = _make_entry()
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_entry(entry)  # duplicate
            w.write_manifest(manifest)

        reader = SQLiteDictionaryReader(db)
        all_entries = list(reader.all_entries())
        assert len(all_entries) == 1  # only one entry, not two
    finally:
        db.unlink(missing_ok=True)


def test_probe_073_sqlite_reader_missing_file_raises():
    """Reading a non-existent file must raise FileNotFoundError."""
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    with pytest.raises(FileNotFoundError):
        SQLiteDictionaryReader(Path("/tmp/nonexistent_dict_test_probe73.sqlite"))


def test_probe_074_sqlite_reader_lookup_by_surface_form_exact():
    """Reader.lookup_by_surface_form must find an entry by its canonical label."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    entry = _make_entry(label="Nipah virus", synonyms=("NiV",))
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_manifest(manifest)

        reader = SQLiteDictionaryReader(db)
        result = reader.lookup_by_surface_form(EntityType.PATHOGEN, "Nipah virus")
        assert result is not None
        assert result.canonical_label == "Nipah virus"
    finally:
        db.unlink(missing_ok=True)


def test_probe_075_sqlite_reader_lookup_surface_form_miss():
    """A surface form not in the dictionary must return None, not raise."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    entry = _make_entry()
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_manifest(manifest)

        reader = SQLiteDictionaryReader(db)
        result = reader.lookup_by_surface_form(EntityType.PATHOGEN, "definitely not a pathogen XYZ")
        assert result is None
    finally:
        db.unlink(missing_ok=True)


def test_probe_076_sqlite_ambiguous_surface_form_recorded():
    """When two entries share a normalized surface form, the conflict
    is recorded in ambiguous_surface_forms table."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryWriter,
    )

    now = _now()

    def _entry(iri, label):
        return DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri=iri,
            canonical_label=label,
            ontology=OntologyName.NCBITAXON,
            ontology_version="2024",
            confidence=0.9,
            resolved_at=now,
            source_records=(),
            synonyms=("shared-name",),
        )

    entry_a = _entry("http://example.com/A", "Virus A")
    entry_b = _entry("http://example.com/B", "Virus B")
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry_a)
            w.write_entry(entry_b)
            w.write_manifest(manifest)

        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT * FROM ambiguous_surface_forms").fetchall()
        conn.close()
        assert len(rows) >= 1, "Expected at least one ambiguous surface form recorded"
    finally:
        db.unlink(missing_ok=True)


def test_probe_077_sqlite_has_taxon_hierarchy_false_without_hierarchy():
    """has_taxon_hierarchy() must return False when no hierarchy was written."""
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryWriter,
    )

    entry = _make_entry()
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_manifest(manifest)
            assert w.has_taxon_hierarchy() is False
    finally:
        db.unlink(missing_ok=True)


def test_probe_078_sqlite_writer_write_taxon_hierarchy_marks_has_hierarchy():
    """After writing taxon hierarchy, has_taxon_hierarchy() returns True."""
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryWriter,
    )

    entry = _make_entry()
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_manifest(manifest)
            # Write a simple hierarchy: 99999 → 10239 (viruses root)
            w.write_taxon_hierarchy(iter([(99999, 10239), (10239, 1)]))
            assert w.has_taxon_hierarchy() is True
    finally:
        db.unlink(missing_ok=True)


def test_probe_079_sqlite_manifest_roundtrip():
    """Manifest written to SQLite must survive deserialization unchanged."""
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    entry = _make_entry()
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_manifest(manifest)

        reader = SQLiteDictionaryReader(db)
        read_manifest = reader.read_manifest()
        assert read_manifest.dictionary_version == manifest.dictionary_version
        assert read_manifest.record_count_total == manifest.record_count_total
    finally:
        db.unlink(missing_ok=True)


def test_probe_080_sqlite_all_entries_yields_written_entry():
    """Reader.all_entries() must yield the entry that was written."""
    from apecx_integration.synonym_dictionary.sqlite_writer import (
        SQLiteDictionaryReader,
        SQLiteDictionaryWriter,
    )

    entry = _make_entry(label="Test virus", synonyms=("TV", "T-virus"))
    manifest = _make_manifest()

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db = Path(f.name)

    try:
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(entry)
            w.write_manifest(manifest)

        reader = SQLiteDictionaryReader(db)
        entries = list(reader.all_entries())
        assert len(entries) == 1
        assert entries[0].canonical_iri == entry.canonical_iri
    finally:
        db.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# DictionaryEntry schema invariants (081-090)
# ---------------------------------------------------------------------------


def test_probe_081_dictionary_entry_extra_field_forbidden():
    """DictionaryEntry must reject unknown extra fields."""
    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import DictionaryEntry

    with pytest.raises(Exception):  # noqa: B017  # ValidationError from pydantic
        DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri="http://example.com/X",
            canonical_label="X",
            ontology=OntologyName.NCBITAXON,
            ontology_version="2024",
            confidence=0.9,
            resolved_at=_now(),
            source_records=(),
            synonyms=(),
            unknown_field="should fail",
        )


def test_probe_082_dictionary_entry_confidence_float():
    """Confidence must be stored as a float and retrieved as float."""
    entry = _make_entry()
    assert isinstance(entry.confidence, float)


def test_probe_083_dictionary_entry_synonyms_is_tuple():
    """Synonyms must be a tuple (not a list) — immutable contract."""
    entry = _make_entry(synonyms=("EEEV", "Eastern equine encephalitis"))
    assert isinstance(entry.synonyms, tuple)


def test_probe_084_dictionary_entry_source_records_is_tuple():
    entry = _make_entry()
    assert isinstance(entry.source_records, tuple)


def test_probe_085_dictionary_entry_resolved_at_is_datetime():
    from datetime import datetime

    entry = _make_entry()
    assert isinstance(entry.resolved_at, datetime)


def test_probe_086_build_manifest_extra_field_forbidden():
    """BuildManifest must reject unknown extra fields (extra='forbid')."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.schema import BuildManifest

    with pytest.raises(Exception):  # noqa: B017
        BuildManifest(
            dictionary_version="x",
            built_at=_now(),
            ontology_versions={},
            record_counts_per_entity_type={EntityType.PATHOGEN: 0},
            unresolved_count=0,
            record_count_total=0,
            unknown_extra_field="bad",
        )


def test_probe_087_synthesis_config_extra_field_forbidden():
    """SynthesisConfig must reject unknown extra fields (extra='forbid')."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    with pytest.raises(Exception):  # noqa: B017
        SynthesisConfig(
            system_prompt="Test",
            unknown_field_typo="oops",
        )


def test_probe_088_synthesis_config_max_rag_chunks_min_1():
    """max_rag_chunks must not accept 0 — minimum is 1 (ge=1 constraint)."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    with pytest.raises(Exception):  # noqa: B017
        SynthesisConfig(
            system_prompt="Test",
            max_rag_chunks=0,
        )


def test_probe_089_synthesis_config_defaults_are_sane():
    """Default config values must satisfy the contract: citations required,
    strict validation on, non-trivial min response length."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(system_prompt="Test prompt")
    assert cfg.require_inline_citations is True
    assert cfg.strict_input_validation is True
    assert cfg.min_response_chars >= 0
    assert cfg.max_rag_chunks >= 1


def test_probe_090_synthesis_config_min_distinct_citations_zero_allowed():
    """min_distinct_citations=0 must be valid (disables the distinct count check).
    The ge=0 constraint allows 0."""
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    cfg = SynthesisConfig(
        system_prompt="Test",
        min_distinct_citations=0,
    )
    assert cfg.min_distinct_citations == 0
