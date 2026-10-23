"""Ambiguous organism names must pause, not resolve to a coin-flip taxon.

``inverse_index`` holds one row per surface form (last write wins); the
displaced conflicts live in ``ambiguous_surface_forms``. Reading only the
former made ``literature_qa(organism="RSV")`` ground on *Tenuivirus
oryzaclavatae* — a rice virus — with ``status="ok"``. Worse, the gazetteer was
built from the same table, so abstracts were stamped with the same wrong IRI
and the identifier filter happily agreed with itself.

These tests pin both halves of the fix: the resolver reports candidates, and
the gazetteer abstains on surfaces it cannot disambiguate.
"""

from __future__ import annotations

import sqlite3

import pytest

from apecx_integration.agents.literature.gazetteer import build_from_dictionary
from apecx_integration.agents.literature.resolve import (
    resolve_organism,
    resolve_organism_to_iri,
)

_RSV_HUMAN = "http://purl.obolibrary.org/obo/NCBITaxon_11250"
_RSV_ROUS = "http://purl.obolibrary.org/obo/NCBITaxon_11886"
_RSV_RICE = "http://purl.obolibrary.org/obo/NCBITaxon_3052763"
_CHIKV = "http://purl.obolibrary.org/obo/NCBITaxon_37124"


def _write_inverse_index(conn: sqlite3.Connection) -> None:
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
            # 'rsv' kept the rice virus — the real dictionary's last writer.
            ("pathogen", "rsv", _RSV_RICE),
            ("pathogen", "chikv", _CHIKV),
            ("pathogen", "chikungunya virus", _CHIKV),
        ],
    )


def _write_entries(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE entries ("
        "entity_type TEXT NOT NULL, canonical_iri TEXT NOT NULL, "
        "canonical_label TEXT NOT NULL, PRIMARY KEY (entity_type, canonical_iri))"
    )
    conn.executemany(
        "INSERT INTO entries VALUES (?, ?, ?)",
        [
            ("pathogen", _RSV_HUMAN, "human respiratory syncytial virus"),
            ("pathogen", _RSV_ROUS, "Rous sarcoma virus"),
            ("pathogen", _RSV_RICE, "Tenuivirus oryzaclavatae"),
            ("pathogen", _CHIKV, "Chikungunya virus"),
        ],
    )


def _write_ambiguous(conn: sqlite3.Connection) -> None:
    """DDL mirrors ``synonym_dictionary.sqlite_writer``."""
    conn.execute(
        "CREATE TABLE ambiguous_surface_forms ("
        "entity_type TEXT NOT NULL, "
        "surface_form_normalized TEXT NOT NULL, "
        "winning_canonical_iri TEXT NOT NULL, "
        "alternative_canonical_iri TEXT NOT NULL, "
        "PRIMARY KEY (entity_type, surface_form_normalized, alternative_canonical_iri))"
    )
    conn.executemany(
        "INSERT INTO ambiguous_surface_forms VALUES (?, ?, ?, ?)",
        [
            ("pathogen", "rsv", _RSV_RICE, _RSV_HUMAN),
            ("pathogen", "rsv", _RSV_RICE, _RSV_ROUS),
        ],
    )


@pytest.fixture
def ambiguous_db(tmp_path) -> str:
    """A dictionary where 'rsv' collides across three taxa and 'chikv' does not."""
    db_path = tmp_path / "dict.sqlite"
    with sqlite3.connect(db_path) as conn:
        _write_inverse_index(conn)
        _write_entries(conn)
        _write_ambiguous(conn)
    return str(db_path)


@pytest.fixture
def legacy_db(tmp_path) -> str:
    """A pre-ambiguity-pass dictionary: no ``ambiguous_surface_forms`` table."""
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db_path) as conn:
        _write_inverse_index(conn)
        _write_entries(conn)
    return str(db_path)


# --------------------------------------------------------------------------
# resolve_organism
# --------------------------------------------------------------------------


def test_ambiguous_term_reports_candidates_and_withholds_iri(ambiguous_db):
    result = resolve_organism("RSV", ambiguous_db)

    assert result.status == "ambiguous"
    # The IRI is withheld deliberately: a caller that ignores `status` must not
    # silently filter a corpus on one arbitrary sense.
    assert result.iri is None
    iris = {c["canonical_iri"] for c in result.candidates}
    assert iris == {_RSV_RICE, _RSV_HUMAN, _RSV_ROUS}


def test_candidate_shape_matches_detect_ambiguity(ambiguous_db):
    """Downstream envelope builders consume either source unchanged."""
    for candidate in resolve_organism("RSV", ambiguous_db).candidates:
        assert set(candidate) == {
            "canonical_iri",
            "canonical_label",
            "canonical_ontology",
            "confidence",
        }
    labels = {
        c["canonical_iri"]: c["canonical_label"]
        for c in resolve_organism("RSV", ambiguous_db).candidates
    }
    assert labels[_RSV_ROUS] == "Rous sarcoma virus"


def test_unambiguous_term_still_resolves(ambiguous_db):
    result = resolve_organism("CHIKV", ambiguous_db)
    assert (result.status, result.iri) == ("resolved", _CHIKV)


def test_unknown_term_is_a_miss_not_an_error(ambiguous_db):
    result = resolve_organism("not-a-virus-xyzzy", ambiguous_db)
    assert (result.status, result.iri, result.candidates) == ("miss", None, [])


def test_chosen_iri_short_circuits_so_the_pause_can_be_answered(ambiguous_db):
    """Re-calling with a candidate IRI must resolve, else the pause is a dead end."""
    result = resolve_organism(_RSV_HUMAN, ambiguous_db)
    assert (result.status, result.iri) == ("resolved", _RSV_HUMAN)


def test_dictionary_without_ambiguity_table_degrades_not_raises(legacy_db):
    result = resolve_organism("RSV", legacy_db)
    assert result.status == "resolved"
    assert result.iri == _RSV_RICE


# --------------------------------------------------------------------------
# resolve_organism_to_iri — the back-compat wrapper
# --------------------------------------------------------------------------


def test_wrapper_fails_closed_on_ambiguity(ambiguous_db):
    """Was returning the rice virus for 'RSV'; must now return None."""
    assert resolve_organism_to_iri("RSV", ambiguous_db) is None


def test_wrapper_unchanged_for_unambiguous_terms(ambiguous_db):
    assert resolve_organism_to_iri("CHIKV", ambiguous_db) == _CHIKV


# --------------------------------------------------------------------------
# gazetteer
# --------------------------------------------------------------------------


def test_gazetteer_abstains_on_ambiguous_surfaces(ambiguous_db):
    gaz = build_from_dictionary(ambiguous_db)

    assert gaz.tag("RSV infection in infants") == []
    # ...while unambiguous surfaces are still stamped.
    assert [t.iri for t in gaz.tag("CHIKV outbreak")] == [_CHIKV]


def test_gazetteer_exclusion_is_opt_out(ambiguous_db):
    gaz = build_from_dictionary(ambiguous_db, exclude_ambiguous=False)
    assert [t.iri for t in gaz.tag("RSV infection")] == [_RSV_RICE]


def test_gazetteer_without_ambiguity_table_degrades_not_raises(legacy_db):
    gaz = build_from_dictionary(legacy_db)
    assert [t.iri for t in gaz.tag("CHIKV outbreak")] == [_CHIKV]
