"""Smoke tests for building the literature gazetteer from dictionary.sqlite.

The fast tests use a tiny fixture sqlite matching the real ``inverse_index``
schema — they never touch the 771MB dictionary. One gated slow test runs
against the real file when present and is skipped by default.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from apecx_integration.agents.literature.gazetteer import build_from_dictionary

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_REAL_DB = "/Users/onarykov/Downloads/apecx-cowork/dictionary.sqlite"


@pytest.fixture
def fixture_db(tmp_path):
    """A minimal inverse_index with two admissible and two guarded rows."""
    db_path = tmp_path / "dict.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE inverse_index ("
            "entity_type TEXT NOT NULL, "
            "surface_form_normalized TEXT NOT NULL, "
            "canonical_iri TEXT NOT NULL, "
            "PRIMARY KEY (entity_type, surface_form_normalized))"
        )
        conn.executemany(
            "INSERT INTO inverse_index VALUES (?, ?, ?)",
            [
                ("pathogen", "chikv", _CHIKV_IRI),
                ("pathogen", "chikungunya virus", _CHIKV_IRI),
                ("pathogen", "the", "http://purl.obolibrary.org/obo/NCBITaxon_1"),
                ("pathogen", "12", "http://purl.obolibrary.org/obo/NCBITaxon_2"),
            ],
        )
    return str(db_path)


def test_builds_and_tags_known_surface(fixture_db):
    gaz = build_from_dictionary(fixture_db)

    tags = gaz.tag("CHIKV neutralizing antibodies")

    assert any(t.iri.endswith("NCBITaxon_37124") for t in tags)


def test_precision_guard_drops_generic_and_numeric_surfaces(fixture_db):
    gaz = build_from_dictionary(fixture_db)

    assert "the" not in gaz._map
    assert "12" not in gaz._map


def test_limit_bounds_rows_read(fixture_db):
    gaz = build_from_dictionary(fixture_db, limit=1)

    assert len(gaz._map) <= 1


@pytest.mark.skipif(not os.path.exists(_REAL_DB), reason="real 771MB dictionary.sqlite not present")
def test_builds_from_real_dictionary_bounded():
    gaz = build_from_dictionary(_REAL_DB, limit=5000)

    assert len(gaz._map) > 0
    # Tag a surface that is actually in the bounded build (row order in the
    # first 5000 rows is not guaranteed, so pick a known-present key).
    known_surface = next(iter(gaz._map))
    assert gaz.tag(known_surface)
