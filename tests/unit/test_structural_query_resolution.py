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


def test_species_name_resolves_arbitrary_virus_without_curated_taxon():
    """An arbitrary virus (taxon NOT in the curated map) taxon-locks via the canonical
    species name the BV-BRC resolver produced — the resolved_species_name path."""
    res = resolve_species_terms(
        "HIV-1 envelope glycoprotein conserved epitopes",
        taxon_id=11676,  # HIV-1, not in _TAXON_SPECIES
        species_name="Human immunodeficiency virus 1",
    )
    assert res.note is None
    assert "human immunodeficiency virus 1" in res.terms
    assert "Human immunodeficiency virus 1" in res.names


def test_curated_bridge_resolves_sars_and_influenza_by_taxon_id():
    """Regression (SARS-CoV-2 spike e2e, 2026-06-27): SARS-CoV-2 + influenza A are NOT named
    "<X> virus" so the query-text parser can't resolve them, and resolved_species_name isn't carried
    in every run — so their structural leg silently DEGRADED (0 structures, no SASA). The curated
    bridge now resolves them by taxon_id alone to the FULL PDB scientific name (facet-precise:
    SARS-CoV-2 excludes SARS-CoV-1; "influenza a virus" is the A-variants, not the broad mix)."""
    sars = resolve_species_terms("spike glycoprotein epitopes", taxon_id=2697049)
    assert sars.note is None
    assert "severe acute respiratory syndrome coronavirus 2" in sars.terms
    flu = resolve_species_terms("hemagglutinin epitopes", taxon_id=11320)
    assert flu.note is None
    assert "influenza a virus" in flu.terms


def test_species_name_keyword_residual_excludes_species_words():
    # The structural keyword residual drops the species-name words, keeping the protein terms.
    kw = _structural_keyword_tokens(
        "severe acute respiratory syndrome coronavirus 2 spike glycoprotein",
        ["severe acute respiratory syndrome coronavirus 2"],
    )
    assert "spike" in kw and "glycoprotein" in kw
    assert "coronavirus" not in kw


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


# --- advanced-query escaping (a reserved-char term used to malform the query → 0 structures) ---


def test_quote_advanced_phrase_makes_reserved_chars_literal():
    from apecx_integration.agents.globus_search.structural_query import _quote_advanced_phrase

    # parentheses / slashes / colons are literal INSIDE quotes — just wrapped
    assert _quote_advanced_phrase("a/puerto rico/8/1934(h1n1)") == '"a/puerto rico/8/1934(h1n1)"'
    # embedded quotes + backslashes are escaped so the phrase itself can't be broken
    assert _quote_advanced_phrase('say "hi"') == '"say \\"hi\\""'
    assert _quote_advanced_phrase("a\\b") == '"a\\\\b"'


def test_emdb_query_is_wellformed_for_reserved_char_species(monkeypatch):
    """A strain label with parentheses/slashes must NOT malform the EMDB advanced
    query_string — the bug that silently returned zero structures before escaping."""
    from apecx_integration.agents.globus_search import client as globus_client
    from apecx_integration.agents.globus_search import structural_query

    captured: dict[str, str] = {}
    monkeypatch.setattr(globus_client, "search", lambda q, **kw: captured.setdefault("q", q) and [])
    strain = "Influenza A virus (A/Puerto Rico/8/1934(H1N1))"
    structural_query.search_one_source(
        "hemagglutinin epitopes", "emdb", "Electron Microscopy Data Bank", species_name=strain
    )
    q = captured["q"]
    assert structural_query._quote_advanced_phrase(strain.strip().lower()) in q
    assert q.count('"') % 2 == 0  # every quote balanced → parser cannot choke


def test_pdb_facet_scopes_with_a_quoted_phrase(monkeypatch):
    from apecx_integration.agents.globus_search import client as globus_client
    from apecx_integration.agents.globus_search import structural_query

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        globus_client, "facet", lambda field, q, **kw: captured.setdefault("q", q) and []
    )
    structural_query.enumerate_organisms(["mayaro virus"], publisher="RCSB PDB")
    assert captured["q"] == structural_query._quote_advanced_phrase("mayaro virus")
