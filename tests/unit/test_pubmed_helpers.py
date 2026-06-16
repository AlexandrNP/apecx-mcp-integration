"""Unit tests for the stateless PubMed helper utilities.

Pins ``build_term`` + ``entity_name`` + ``container_to_dict`` behavior
without hitting the network. The actual ``harvest`` async function
exercises real eUtils and is integration-tested separately.
"""

from __future__ import annotations

from types import SimpleNamespace

from apecx_integration.composition.steps._pubmed_helpers import (
    build_focused_term,
    build_term,
    container_to_dict,
    entity_name,
)

_RESOLVER = "apecx_integration.agents.globus_search.taxonomy_resolver.extract_virus_names"

# ---------------------------------------------------------------------
# entity_name
# ---------------------------------------------------------------------


def test_entity_name_from_str():
    assert entity_name("EEEV") == "EEEV"


def test_entity_name_from_str_strips_whitespace():
    assert entity_name("  Sindbis  ") == "Sindbis"


def test_entity_name_from_empty_str_returns_none():
    assert entity_name("") is None
    assert entity_name("   ") is None


def test_entity_name_from_dict_with_name():
    assert entity_name({"name": "EEEV", "type": "pathogen"}) == "EEEV"


def test_entity_name_from_dict_without_name_returns_none():
    assert entity_name({"type": "pathogen"}) is None


def test_entity_name_unrecognized_shape_returns_none():
    assert entity_name(42) is None
    assert entity_name(None) is None
    assert entity_name(["EEEV"]) is None


# ---------------------------------------------------------------------
# build_term
# ---------------------------------------------------------------------


def test_build_term_default_passes_query_through():
    assert build_term("EEEV vaccine", None) == "EEEV vaccine"


def test_build_term_substitutes_query_placeholder():
    out = build_term("EEEV", None, template="{query} AND vaccine[Title]")
    assert out == "EEEV AND vaccine[Title]"


def test_build_term_substitutes_entities_placeholder():
    out = build_term(
        "vaccines",
        [{"name": "EEEV"}, {"name": "WEEV"}],
        template="{query} for {entities}",
    )
    assert out == "vaccines for EEEV, WEEV"


def test_build_term_handles_str_entities():
    out = build_term(
        "vaccines",
        ["EEEV", "Sindbis"],
        template="{query} {entities}",
    )
    assert out == "vaccines EEEV, Sindbis"


def test_build_term_skips_unrecognized_entity_shapes():
    out = build_term(
        "vaccines",
        ["EEEV", 42, None, {"type": "pathogen"}, {"name": "valid"}],
        template="{query}: {entities}",
    )
    # 42, None, dict-without-name → all dropped silently.
    assert out == "vaccines: EEEV, valid"


def test_build_term_falls_back_on_template_format_error(caplog):
    """Bad template (unknown placeholder) → return raw query, log WARNING."""
    with caplog.at_level("WARNING"):
        out = build_term(
            "EEEV",
            None,
            template="{unknown_field}",
            owner_name="my_step",
        )
    assert out == "EEEV"
    # WARNING includes owner_name so an operator can correlate.
    assert any("my_step:" in r.message for r in caplog.records)


def test_build_term_handles_empty_entities_list():
    """Empty entities → ``{entities}`` becomes empty string, not error."""
    out = build_term("Q", [], template="{query} {entities}")
    # Trailing space is fine; the term is what eSearch sees.
    assert out == "Q "


# ---------------------------------------------------------------------
# build_focused_term — organism-anchored eSearch term construction
#
# Pure-transform (no network): the only dependency is the LOCAL virus-name
# resolver, which we pin via monkeypatch for determinism. The real eUtils
# behaviour (that the produced term returns non-empty results) is integration-
# covered by tests/integration/test_viral_epitope_analysis.py's end-to-end CHIKV
# run, which drives the assembly step's PubMed branch against live PubMed.
# ---------------------------------------------------------------------


def test_build_focused_term_anchors_on_virus_and_or_groups_concepts(monkeypatch):
    """The bug: the verbose raw query ANDs every token → 0 hits. The fix anchors
    on the virus name (AND) and OR-groups the remaining concept tokens."""
    monkeypatch.setattr(_RESOLVER, lambda q: ["Chikungunya virus"])
    out = build_focused_term(
        "conserved chikungunya structural polyprotein epitopes and structural references"
    )
    # Organism is the phrase-quoted AND anchor; concepts are an OR group.
    assert out == (
        '"Chikungunya virus" AND (conserved OR structural OR polyprotein OR epitopes OR references)'
    )


def test_build_focused_term_excludes_organism_words_boolean_and_short_tokens(monkeypatch):
    """Organism words (chikungunya, virus), the bareword boolean ``and``, and
    sub-3-char tokens never leak into the concept OR-group."""
    monkeypatch.setattr(_RESOLVER, lambda q: ["Chikungunya virus"])
    out = build_focused_term("chikungunya E1 of the virus and epitopes")
    concepts = out.split("AND (", 1)[1].rstrip(")")
    tokens = [t.strip() for t in concepts.split(" OR ")]
    assert "and" not in tokens  # boolean operator dropped
    assert "E1" not in tokens  # < 3 chars dropped
    assert "virus" not in tokens and "chikungunya" not in tokens  # organism words dropped
    assert "epitopes" in tokens and "the" in tokens  # >=3-char non-organism tokens kept


def test_build_focused_term_dedups_concepts_case_insensitively(monkeypatch):
    monkeypatch.setattr(_RESOLVER, lambda q: ["Chikungunya virus"])
    out = build_focused_term("chikungunya Structural structural STRUCTURAL polyprotein")
    # 'structural' appears once despite three casings.
    assert out == '"Chikungunya virus" AND (Structural OR polyprotein)'


def test_build_focused_term_multiple_organisms_or_grouped_anchor(monkeypatch):
    monkeypatch.setattr(_RESOLVER, lambda q: ["Chikungunya virus", "Dengue virus"])
    out = build_focused_term("chikungunya and dengue epitopes")
    assert out == '("Chikungunya virus" OR "Dengue virus") AND (epitopes)'


def test_build_focused_term_anchor_only_when_no_concept_tokens(monkeypatch):
    monkeypatch.setattr(_RESOLVER, lambda q: ["Chikungunya virus"])
    out = build_focused_term("chikungunya virus")
    assert out == '"Chikungunya virus"'


def test_build_focused_term_no_virus_falls_back_to_raw_query(monkeypatch):
    """No anchor in the query → return the raw query unchanged (never worse)."""
    monkeypatch.setattr(_RESOLVER, lambda q: [])
    assert build_focused_term("generic envelope glycoprotein epitopes") == (
        "generic envelope glycoprotein epitopes"
    )


def test_build_focused_term_resolver_error_degrades_to_raw_query(monkeypatch, caplog):
    """A resolver/import failure must degrade LOUD to the raw query, not crash."""

    def _boom(_q):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(_RESOLVER, _boom)
    with caplog.at_level("WARNING"):
        out = build_focused_term("chikungunya epitopes", owner_name="assemble")
    assert out == "chikungunya epitopes"
    assert any("assemble:" in r.message for r in caplog.records)


def test_build_focused_term_uses_real_resolver_for_known_virus():
    """Integration-lite: the REAL local resolver anchors a CHIKV query (no mock,
    no network) — proves the wiring, not just the mocked structure."""
    out = build_focused_term("chikungunya structural polyprotein epitopes")
    assert out.startswith('"Chikungunya virus" AND (')
    assert "polyprotein" in out and "epitopes" in out


# ---------------------------------------------------------------------
# container_to_dict — DataCite shape projection
# ---------------------------------------------------------------------


def _make_container(**overrides):
    """Build a fake container with arbitrary attributes via SimpleNamespace."""
    defaults = {
        "titles": [],
        "creators": [],
        "publicationYear": None,
        "dates": [],
        "publisher": None,
        "identifier": None,
        "alternateIdentifiers": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_container_to_dict_minimal_returns_empty_strings():
    """A container missing every optional field still produces a
    well-shaped dict — none of the keys raise."""
    out = container_to_dict(_make_container())
    assert out == {
        "doi": "",
        "title": "",
        "authors": [],
        "year": "",
        "journal": "",
        "pmid": "",
    }


def test_container_to_dict_first_title_only():
    container = _make_container(
        titles=[
            SimpleNamespace(title="Primary title"),
            SimpleNamespace(title="Secondary title"),
        ]
    )
    out = container_to_dict(container)
    assert out["title"] == "Primary title"


def test_container_to_dict_uses_creator_name_when_present():
    container = _make_container(creators=[SimpleNamespace(name="Doe, J.")])
    out = container_to_dict(container)
    assert out["authors"] == ["Doe, J."]


def test_container_to_dict_assembles_family_given_when_no_name():
    container = _make_container(
        creators=[SimpleNamespace(name=None, familyName="Doe", givenName="Jane")]
    )
    out = container_to_dict(container)
    assert out["authors"] == ["Doe, Jane"]


def test_container_to_dict_falls_back_to_just_family():
    container = _make_container(
        creators=[SimpleNamespace(name=None, familyName="Doe", givenName=None)]
    )
    out = container_to_dict(container)
    assert out["authors"] == ["Doe"]


def test_container_to_dict_caps_authors_with_et_al_marker():
    """Hard cap at 25 authors with an "et al." marker for the rest."""
    creators = [SimpleNamespace(name=f"Author{i}") for i in range(50)]
    container = _make_container(creators=creators)
    out = container_to_dict(container)
    assert len(out["authors"]) == 26  # 25 + 1 marker
    assert out["authors"][-1] == "et al. (25 more)"


def test_container_to_dict_year_from_dates_when_publication_year_missing():
    container = _make_container(
        dates=[SimpleNamespace(date="2024-03-15")],
    )
    out = container_to_dict(container)
    assert out["year"] == "2024"


def test_container_to_dict_publication_year_takes_priority():
    container = _make_container(
        publicationYear=2025,
        dates=[SimpleNamespace(date="2020")],
    )
    out = container_to_dict(container)
    assert out["year"] == "2025"


def test_container_to_dict_pmid_from_alternate_identifiers():
    container = _make_container(
        alternateIdentifiers=[
            SimpleNamespace(alternateIdentifierType="DOI", alternateIdentifier="10.1/x"),
            SimpleNamespace(alternateIdentifierType="PMID", alternateIdentifier="12345"),
        ]
    )
    out = container_to_dict(container)
    assert out["pmid"] == "12345"


def test_container_to_dict_doi_from_identifier():
    container = _make_container(identifier=SimpleNamespace(identifier="10.1234/abc"))
    out = container_to_dict(container)
    assert out["doi"] == "10.1234/abc"


def test_container_to_dict_journal_from_publisher_name():
    container = _make_container(publisher=SimpleNamespace(name="Nature"))
    out = container_to_dict(container)
    assert out["journal"] == "Nature"
