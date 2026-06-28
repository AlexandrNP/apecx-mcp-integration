"""Regression for extract_virus_names phrase extraction (2026-06-27 alphavirus-probe finding).

The greedy "<X> virus" phrase window captures up to 4 words before "virus", so natural phrasing
("...epitopes on the Eastern equine encephalitis virus") prepends the article / sentence context
("the Eastern equine encephalitis") — which then MISSES the article-free dictionary key and the whole
(NON-aliased) virus fails to resolve. The fix emits every trailing SUFFIX (longest-first) so the
caller resolves the most-specific real "<name> virus". Aliased names match the alias table directly
and are unaffected.
"""

from __future__ import annotations

from apecx_integration.agents.globus_search.taxonomy_resolver import extract_virus_names


def test_strips_leading_article_from_three_word_name():
    out = extract_virus_names(
        "conserved surface-exposed epitopes on the Eastern equine encephalitis virus E2 glycoprotein"
    )
    # the clean, article-free, dict-resolvable form is the PRIMARY candidate (no leading "the"),
    # so downstream consumers that use names[0] get the real name.
    assert out[0] == "Eastern equine encephalitis virus"
    assert not any(c.lower().startswith("the ") for c in out)


def test_strips_sentence_context_from_one_word_name():
    # A 1-word name: the greedy window prepends CONTENT words ("epitopes on the Sindbis"); the clean
    # "Sindbis virus" suffix must still be emitted so resolution succeeds.
    out = extract_virus_names("conserved epitopes on the Sindbis virus capsid protein")
    assert "Sindbis virus" in out


def test_suffixes_are_longest_first():
    # Most-specific first so the caller's first-that-resolves wins the real name over a short suffix.
    out = extract_virus_names("epitopes on the Venezuelan equine encephalitis virus nsP2 protease")
    full = "Venezuelan equine encephalitis virus"
    short = "encephalitis virus"
    assert full in out and short in out
    assert out.index(full) < out.index(short)


def test_abbreviated_name_with_period_survives():
    # "St." must stay attached — the dict key IS "St. Louis encephalitis virus"; dropping the period
    # (window starting at "Louis") misses it entirely. 2026-06-28 diverse-virus-probe finding.
    out = extract_virus_names(
        "conserved epitopes on the St. Louis encephalitis virus NS5 polymerase"
    )
    assert "St. Louis encephalitis virus" in out
    assert out[0] == "St. Louis encephalitis virus"


def test_aliased_virus_unaffected():
    # Alias-table canonical spelling stays the highest-priority candidate.
    out = extract_virus_names("epitopes on the chikungunya virus E1")
    assert "Chikungunya virus" in out
    assert out[0] == "Chikungunya virus"


def test_virology_acronyms_resolve_via_alias_table():
    # A bare acronym previously produced NO candidate (no alias, no "<X> virus" phrase, no suffix) →
    # acronym-only queries MISSED even when the dict knew the taxon. 2026-06-28 acronym-probe finding.
    for acr, expect in [
        ("EEEV", "Eastern equine encephalitis virus"),
        ("LASV", "Lassa mammarenavirus"),
        ("NiV", "Nipah henipavirus"),
        ("CCHFV", "Crimean-Congo hemorrhagic fever virus"),
        ("MPXV", "Monkeypox virus"),
    ]:
        assert expect in extract_virus_names(f"conserved epitopes on the {acr} glycoprotein")


def test_ambiguous_acronyms_deliberately_omitted():
    # SFV (Semliki Forest vs Shope fibroma) + HEV (Hepatitis E vs Hendra) are NOT alias-mapped — the
    # alias table must not assert a single canonical for them. The FULL name still works (phrase path).
    assert "Semliki Forest virus" not in extract_virus_names("epitopes on the SFV E1 protein")
    assert "Semliki Forest virus" in extract_virus_names("epitopes on the Semliki Forest virus E1")


def test_no_false_positive_on_non_virus_caps():
    # \b boundaries must keep acronyms from matching inside ordinary words / DNA/RNA mentions.
    assert extract_virus_names("conserved epitopes analysis of DNA and RNA structure") == []
