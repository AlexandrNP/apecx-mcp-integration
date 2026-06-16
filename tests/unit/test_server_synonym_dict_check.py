"""
Unit tests for the ``_ensure_synonym_dict_or_warn`` startup gate.

Scenarios:
  1. APECX_SYNONYM_DICT_PATH not set, no data    → build attempt skipped,
     warning that VIOLIN data is missing.
  2. Path set but file missing, no data          → same as 1, warning at the
     specific path the operator chose.
  3. Path set + file exists but manifest corrupt → load error banner.
  4. Path set + valid SQLite dictionary          → silent success, singleton warm.

The new behavior (Phase 3 of dictionary-build-as-workflow) replaced
"warn the operator to run apecx-build-dictionary" with "invoke the
build workflow itself unless inputs are missing or the operator opted
out." Scenarios 1 and 2 used to assert specific banner content; they
now assert the workflow-skip-with-reason path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apecx_integration.mcp_surface.server import _ensure_synonym_dict_or_warn
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
# Scenario 1: env var absent + no data → build skipped, fallback warning
# ---------------------------------------------------------------------------


def test_download_unavailable_fails_loud_does_not_silently_build(monkeypatch, tmp_path, caplog):
    """Download is the ONLY auto path (2026-06-15): when it produces no file and the local build
    is NOT opted in, the server FAILS LOUD ('dictionary unavailable') — it does NOT silently fall
    back to a build (the old behavior that masked a download failure). Download is suppressed via
    ``APECX_SKIP_DICT_DOWNLOAD=1`` to stay offline."""
    monkeypatch.setenv("APECX_SKIP_DICT_DOWNLOAD", "1")
    monkeypatch.delenv("APECX_DICT_ALLOW_LOCAL_BUILD", raising=False)
    monkeypatch.setenv("APECX_DATA_ROOT", str(tmp_path))  # empty dir, no violin/
    monkeypatch.setenv("APECX_DICT_OUTPUT_DIR", str(tmp_path / "out"))
    with caplog.at_level(logging.WARNING):
        _ensure_synonym_dict_or_warn()
    text = caplog.text.lower()
    assert "dictionary unavailable" in text  # the loud, actionable message
    assert "globus download" in text
    # did NOT silently attempt a build
    assert "invoking local-build" not in text and "allow_local_build" in text  # build is opt-in


# ---------------------------------------------------------------------------
# Scenario 1b: APECX_SKIP_DICT_BUILD=1 honored
# ---------------------------------------------------------------------------


def test_opt_in_local_build_runs_when_allowed(monkeypatch, tmp_path, caplog):
    """The local build is OPT-IN: with APECX_DICT_ALLOW_LOCAL_BUILD=1 (and the download
    suppressed), the build IS attempted (it then skips on missing VIOLIN data, but the opt-in
    path was taken — proving build is reachable for dev/offline)."""
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(missing))
    monkeypatch.setenv("APECX_SKIP_DICT_DOWNLOAD", "1")
    monkeypatch.setenv("APECX_DICT_ALLOW_LOCAL_BUILD", "1")
    monkeypatch.setenv("APECX_DATA_ROOT", str(tmp_path / "no_data"))  # no violin → build skips
    with caplog.at_level(logging.INFO):
        _ensure_synonym_dict_or_warn()
    text = caplog.text.lower()
    assert "allow_local_build=1" in text  # the opt-in build path was entered


# ---------------------------------------------------------------------------
# Scenario 2: env var set but file missing + no data → build skip + warning
# ---------------------------------------------------------------------------


def test_missing_file_no_data_skips_build(monkeypatch, tmp_path, caplog):
    """Operator pointed APECX_SYNONYM_DICT_PATH at a non-existent file and
    has no VIOLIN data either: build attempted, skipped for missing data,
    fallback warning emitted.

    Public download is suppressed via ``APECX_SKIP_DICT_DOWNLOAD=1`` so
    this test stays offline; the download-then-build cascade is covered
    by ``test_clean_install_dict_bootstrap.py``.
    """
    missing = tmp_path / "does_not_exist.sqlite"
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(missing))
    monkeypatch.setenv("APECX_SKIP_DICT_DOWNLOAD", "1")
    monkeypatch.delenv("APECX_DICT_ALLOW_LOCAL_BUILD", raising=False)
    monkeypatch.setenv("APECX_DATA_ROOT", str(tmp_path / "no_data"))  # not present
    with caplog.at_level(logging.WARNING):
        _ensure_synonym_dict_or_warn()
    text = caplog.text.lower()
    assert "dictionary unavailable" in text  # loud + actionable, not a silent build/degrade


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
        _ensure_synonym_dict_or_warn()
    assert "failed to load" in caplog.text.lower() or "fallback" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Scenario 4: valid dictionary — silent success + singleton pre-warmed
# ---------------------------------------------------------------------------


def test_valid_dict_is_silent_and_warms_singleton(monkeypatch, tmp_path, caplog):
    db = tmp_path / "dict.sqlite"
    _write_valid_dict(db)
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(db))

    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _ensure_synonym_dict_or_warn()
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
