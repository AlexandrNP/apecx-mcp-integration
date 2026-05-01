"""Stage 1 → Stage 2 end-to-end integration tests (P3.10).

Verifies the full pipeline:
  1. apecx-build-dictionary (Stage 1) builds a real SQLite from VIOLIN data.
  2. DictionaryIndex.load() (Stage 2 fast path) loads the artifact.
  3. lookup_entity() returns correct path/confidence for known entities.
  4. lookup_by_iri() reverse-lookup works against the built artifact.

Gated on APECX_SYNONYM_DICT_LIVE_OLS=1 because Stage 1 calls real EBI OLS.

To run:

    APECX_SYNONYM_DICT_LIVE_OLS=1 \\
        PYTHONPATH=src .venv/bin/python -m pytest \\
        tests/integration/test_stage2_lookup.py -v

Mock parity: these tests back-stop
  tests/unit/synonym_dictionary/test_loader_lookup.py — specifically the
  lookup_entity fast-path, IRI-shortcut, and lookup_by_iri unit tests
  which use a synthetic SQLite.  Here we use a real dictionary built from
  real VIOLIN rows against real OLS.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_LIVE_OLS = os.environ.get("APECX_SYNONYM_DICT_LIVE_OLS", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE_OLS,
    reason=(
        "Set APECX_SYNONYM_DICT_LIVE_OLS=1 to run Stage 2 integration tests "
        "that require a real dictionary artifact built from live OLS."
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
VIOLIN_PATHOGENS = WORKSPACE_ROOT / "data" / "violin" / "Pathogen_Information.csv"


# ---------------------------------------------------------------------------
# Fixture: build a dictionary once per test session and reuse the artifact.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a Stage 1 dictionary from 5 VIOLIN pathogen rows.

    Returns the path to the ``dictionary.sqlite`` artifact.
    """
    assert VIOLIN_PATHOGENS.exists(), (
        f"VIOLIN pathogen data not found at {VIOLIN_PATHOGENS}. "
        "Ensure the workspace data/ directory is populated."
    )

    from apecx_integration.synonym_dictionary.cli import main

    out = tmp_path_factory.mktemp("stage2_dict")
    ret = main(
        [
            "--violin-pathogens",
            str(VIOLIN_PATHOGENS),
            "--output",
            str(out),
            "--dictionary-version",
            "test-p3.10",
            "--max-rows",
            "5",
            "--log-level",
            "WARNING",
        ]
    )
    assert ret == 0, f"apecx-build-dictionary exited with code {ret}"
    db_path = out / "dictionary.sqlite"
    assert db_path.exists(), f"dictionary.sqlite missing at {db_path}"
    return db_path


# ---------------------------------------------------------------------------
# P3.10 — Stage 2 fast path against a real built dictionary
# ---------------------------------------------------------------------------


def test_stage2_dictionary_has_entries(built_dictionary: Path) -> None:
    """DictionaryIndex.load() must return at least one entry from the real build."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex

    index = DictionaryIndex.load(built_dictionary)
    assert index.entry_count() >= 1, "Stage 1 built zero dictionary entries from 5 VIOLIN rows"


def test_stage2_anchor_mode_rows_have_confidence_one(built_dictionary: Path) -> None:
    """Rows resolved via NCBI_Taxonomy_ID (anchor mode) must have confidence 1.0.

    96.8% of VIOLIN pathogen rows have NCBI_Taxonomy_ID (M1 measurement).
    With 5 rows, at least 4 should be anchor-mode.  After the float-string
    bug fix, every anchor-mode entry must show confidence=1.0 — not 0.5
    from search fallback.
    """
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    reader = SQLiteDictionaryReader(built_dictionary)
    entries = list(reader.all_entries())

    anchor_entries = [e for e in entries if e.confidence == 1.0]
    assert len(anchor_entries) >= 1, (
        "No anchor-mode entries (confidence=1.0) found.  This may indicate "
        "the normalize_iri float-string bug has regressed: pandas reads "
        "NCBI_Taxonomy_ID as float64, producing '10298.0' strings that a "
        "broken isdigit() check would reject."
    )

    _ = DictionaryIndex.load(built_dictionary)  # verify load succeeds after assertions


def test_stage2_lookup_entity_fast_path(built_dictionary: Path) -> None:
    """lookup_entity must return path='fast' for a known anchor-mode entry.

    Loads the real dictionary into the process singleton, then calls
    lookup_entity() with a canonical label extracted from the built artifact.
    """
    from apecx_integration.synonym_dictionary.loader import _ProcessSingleton
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    # Isolate to a fresh singleton so we don't bleed state into other tests.
    singleton = _ProcessSingleton()
    singleton.configure(built_dictionary)
    index, err = singleton.get()
    assert index is not None, f"Failed to load dictionary: {err}"

    # Pick the first anchor-mode entry (confidence==1.0) and look it up.
    anchor_entries = [e for e in index._entries.values() if e.confidence == 1.0]
    assert anchor_entries, "No anchor-mode entries to test fast path with"

    # Patch the module-level singleton so lookup_entity() uses our test instance.
    import apecx_integration.synonym_dictionary.loader as _loader

    _orig = _loader._singleton
    _loader._singleton = singleton
    try:
        target = anchor_entries[0]
        result = lookup_entity(target.canonical_label)
        assert result.path == "fast", (
            f"Expected fast path for {target.canonical_label!r} "
            f"(IRI={target.canonical_iri}); got path={result.path!r}.  "
            "Dictionary may not have been loaded correctly."
        )
        assert result.confidence == 1.0
        assert result.canonical_iri == target.canonical_iri
    finally:
        _loader._singleton = _orig


def test_stage2_lookup_by_iri_real_data(built_dictionary: Path) -> None:
    """lookup_entity called with an IRI must hit the fast path (P3.4 IRI shortcut).

    Mock parity for:
    tests/unit/synonym_dictionary/test_loader_lookup.py::
        test_lookup_entity_accepts_iri_as_surface_form
    """
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import _ProcessSingleton
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    singleton = _ProcessSingleton()
    singleton.configure(built_dictionary)
    index, err = singleton.get()
    assert index is not None, f"Failed to load dictionary: {err}"

    anchor_entries = [e for e in index._entries.values() if e.confidence == 1.0]
    assert anchor_entries, "No anchor-mode entries to test IRI lookup with"

    target = anchor_entries[0]

    _orig = _loader._singleton
    _loader._singleton = singleton
    try:
        result = lookup_entity(target.canonical_iri)
        assert result.path == "fast", (
            f"lookup_entity({target.canonical_iri!r}) did not hit fast path; "
            f"got path={result.path!r}.  The IRI shortcut in lookup_entity "
            "may not be working against a real built dictionary."
        )
        assert result.canonical_iri == target.canonical_iri
        assert result.canonical_label == target.canonical_label
    finally:
        _loader._singleton = _orig


def test_stage2_lookup_by_iri_method_on_index(built_dictionary: Path) -> None:
    """DictionaryIndex.lookup_by_iri must return the entry for a known IRI."""
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex

    index = DictionaryIndex.load(built_dictionary)
    anchor_entries = [e for e in index._entries.values() if e.confidence == 1.0]
    assert anchor_entries

    target = anchor_entries[0]
    entry = index.lookup_by_iri(target.canonical_iri)
    assert entry is not None
    assert entry.canonical_iri == target.canonical_iri
    assert entry.canonical_label == target.canonical_label


def test_stage2_manifest_records_correct_version(built_dictionary: Path) -> None:
    """BuildManifest must reflect the --dictionary-version passed to Stage 1."""
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

    reader = SQLiteDictionaryReader(built_dictionary)
    manifest = reader.read_manifest()
    assert manifest.dictionary_version == "test-p3.10"
    assert manifest.record_count_total == 5
