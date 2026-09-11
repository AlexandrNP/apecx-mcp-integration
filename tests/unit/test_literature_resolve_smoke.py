"""Smoke tests for organism-name -> NCBITaxon IRI resolution.

Fast tests use a tiny fixture sqlite matching the real ``inverse_index``
schema — they never touch the 771MB dictionary. One gated slow test runs
against the real file when present and is skipped by default.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from apecx_integration.agents.literature.resolve import resolve_organism_to_iri

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_REAL_DB = "/Users/onarykov/Downloads/apecx-cowork/dictionary.sqlite"


@pytest.fixture
def fixture_db(tmp_path):
    """A minimal inverse_index with one organism surface form."""
    db_path = tmp_path / "dict.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE inverse_index ("
            "entity_type TEXT NOT NULL, "
            "surface_form_normalized TEXT NOT NULL, "
            "canonical_iri TEXT NOT NULL, "
            "PRIMARY KEY (entity_type, surface_form_normalized))"
        )
        conn.execute(
            "INSERT INTO inverse_index VALUES (?, ?, ?)",
            ("pathogen", "chikv", _CHIKV_IRI),
        )
    return str(db_path)


def test_resolves_known_organism_to_ncbitaxon_iri(fixture_db):
    iri = resolve_organism_to_iri("CHIKV", fixture_db)

    assert iri is not None
    assert iri.endswith("NCBITaxon_37124")


def test_returns_none_on_miss(fixture_db):
    assert resolve_organism_to_iri("not-a-real-organism", fixture_db) is None


@pytest.mark.skipif(not os.path.exists(_REAL_DB), reason="real 771MB dictionary.sqlite not present")
def test_resolves_known_organism_from_real_dictionary():
    iri = resolve_organism_to_iri("Chikungunya virus", _REAL_DB)

    assert iri is not None
    assert iri.startswith("http://purl.obolibrary.org/obo/NCBITaxon_")
