"""
Unit tests for the ``_check_synonym_dict_or_warn`` startup gate.

Scenarios:
  1. APECX_SYNONYM_DICT_PATH not set → loud banner mentioning the env var
  2. Path set but file missing       → loud banner mentioning the path
  3. Path set + file exists but manifest corrupt → load error banner
  4. Path set + valid SQLite dictionary → silent (INFO log only), singleton warm
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from apecx_integration.mcp_surface.server import _check_synonym_dict_or_warn
from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter


def _reset_dict_singleton() -> None:
    import apecx_integration.synonym_dictionary.loader as _loader

    _loader._singleton = _loader._ProcessSingleton()


@pytest.fixture(autouse=True)
def clear_dict_state(monkeypatch):
    monkeypatch.delenv("APECX_SYNONYM_DICT_PATH", raising=False)
    _reset_dict_singleton()
    yield
    _reset_dict_singleton()


def _write_valid_dict(path: Path) -> None:
    entry = DictionaryEntry(
        entity_type=EntityType.PATHOGEN,
        canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        canonical_label="Chikungunya virus",
        synonyms=("CHIKV",),
        ontology=OntologyName.NCBITAXON,
        ontology_version="test-2026",
        source_records=("row.1",),
        confidence=1.0,
        resolved_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    manifest = BuildManifest(
        dictionary_version="test-2026-05",
        built_at=datetime(2026, 5, 1, tzinfo=UTC),
        ontology_versions={"ncbitaxon": "test-2026"},
        record_counts_per_entity_type={EntityType.PATHOGEN: 1},
        unresolved_count=0,
        record_count_total=1,
    )
    with SQLiteDictionaryWriter(path) as w:
        w.write_entry(entry)
        w.write_manifest(manifest)


# ---------------------------------------------------------------------------
# Scenario 1: env var absent
# ---------------------------------------------------------------------------


def test_no_env_var_logs_banner(caplog):
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_synonym_dict_or_warn()
    text = caplog.text
    assert "APECX_SYNONYM_DICT_PATH" in text
    assert "slow" in text.lower() or "fallback" in text.lower()


# ---------------------------------------------------------------------------
# Scenario 2: env var set but file missing
# ---------------------------------------------------------------------------


def test_missing_file_logs_banner(monkeypatch, tmp_path, caplog):
    missing = tmp_path / "does_not_exist.sqlite"
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(missing))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_synonym_dict_or_warn()
    text = caplog.text
    assert str(missing) in text
    assert "not found" in text.lower() or "fallback" in text.lower()


# ---------------------------------------------------------------------------
# Scenario 3: file exists but contains no manifest (corrupt build)
# ---------------------------------------------------------------------------


def test_corrupt_dict_logs_load_error(monkeypatch, tmp_path, caplog):
    bad = tmp_path / "bad.sqlite"
    with SQLiteDictionaryWriter(bad) as w:
        entry = DictionaryEntry(
            entity_type=EntityType.PATHOGEN,
            canonical_iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
            canonical_label="CHIKV",
            synonyms=(),
            ontology=OntologyName.NCBITAXON,
            ontology_version="v0",
            source_records=(),
            confidence=1.0,
            resolved_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        w.write_entry(entry)
        # Deliberately NOT writing manifest — simulates a crashed build.

    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(bad))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_synonym_dict_or_warn()
    assert "failed to load" in caplog.text.lower() or "fallback" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Scenario 4: valid dictionary — silent success + singleton pre-warmed
# ---------------------------------------------------------------------------


def test_valid_dict_is_silent_and_warms_singleton(monkeypatch, tmp_path, caplog):
    db = tmp_path / "dict.sqlite"
    _write_valid_dict(db)
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(db))

    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_synonym_dict_or_warn()
    # No warning banner.
    assert "SYNONYM_DICT" not in caplog.text
    assert "not found" not in caplog.text.lower()
    assert "failed" not in caplog.text.lower()

    # Singleton should be warm — get_dictionary_index returns an index.
    from apecx_integration.synonym_dictionary.loader import (
        DictionaryIndex,
        get_dictionary_index,
    )

    index, err = get_dictionary_index()
    assert err is None
    assert isinstance(index, DictionaryIndex)
