"""Unit tests for the taxon->species resolution + keyword logic (E3-2.2 / E3-2.5).

Pure functions, no network. The facet enumeration + structured query are exercised
against real Globus in tests/integration/test_structural_query_live.py.
"""

from __future__ import annotations

from apecx_integration.agents.globus_search.structural_query import (
    _structural_keyword_tokens,
    resolve_species_terms,
)


def test_taxon_id_37124_resolves_to_chikungunya_virus():
    """E3-2.2 CC-1: a real CHIKV taxon_id resolves to a non-empty CHIKV name set."""
    res = resolve_species_terms("envelope glycoprotein", taxon_id=37124)
    assert res.note is None
    assert "Chikungunya virus" in res.names  # canonical spelling present
    assert "chikungunya" in res.terms  # facet/EMDB scope token
    assert len(res.names) >= 1


def test_taxon_id_accepts_digit_string():
    res = resolve_species_terms("spike", taxon_id="37124")
    assert "chikungunya" in res.terms


def test_query_text_x_virus_phrase_resolves_without_taxon_id():
    res = resolve_species_terms("structures of West Nile virus envelope")
    assert res.note is None
    assert "west nile" in res.terms
    assert any(n == "West Nile virus" for n in res.names)


def test_query_text_curated_token_without_the_word_virus():
    # The verified before/after query carries no literal "virus" word.
    res = resolve_species_terms("chikungunya envelope epitopes")
    assert res.note is None
    assert res.terms == ["chikungunya"]


def test_unresolvable_query_degrades_loud_named_note():
    """E3-2.5 CC-1 degrade: nothing resolvable -> empty terms + a NON-EMPTY note."""
    res = resolve_species_terms("envelope glycoprotein structure")
    assert res.terms == []
    assert res.names == []
    assert res.note is not None and res.note.strip()
    assert "not taxon-locked" in res.note


def test_structural_keywords_strip_species_words():
    assert _structural_keyword_tokens("chikungunya envelope epitopes", ["chikungunya"]) == [
        "envelope",
        "epitopes",
    ]
    # A species-only query yields no residual keyword.
    assert _structural_keyword_tokens("chikungunya virus", ["chikungunya"]) == []
