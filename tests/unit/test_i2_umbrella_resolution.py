"""I2 umbrella-resolution: taxonomic umbrellas resolve to their family node instead of missing.

The (a)/(b) confound experiment showed the big miss-fallback queries (coronavirus etc.) serve on-topic
records that score 0.0 only because the umbrella surface form never resolves, leaving an empty judge subtree.
Option B adds family aliases so the miss-retry (build_resolution_plan -> extract_virus_names -> lookup_entity)
resolves the umbrella to its family taxon. Non-taxonomic umbrellas (hemorrhagic fever / hepatitis virus) are
NOT handled here: the _syndrome_category seam recognizes them but is wired only into the epitope workflow, so
the harmonized-search pipeline still serves them as 0.0-FP misses (Option A follow-up; documented in the test
below).

Real dictionary data (not synthetic): lookup_entity + build_resolution_plan hit the live synonym dictionary.
"""

from __future__ import annotations

import pytest

from apecx_integration.agents.globus_search.taxonomy_resolver import extract_virus_names
from apecx_integration.composition.steps.taxon_candidate_review_step import _syndrome_category

_UMBRELLAS = [
    ("coronavirus", "Coronaviridae", "NCBITaxon_11118"),
    ("poxvirus", "Poxviridae", "NCBITaxon_10240"),
    ("herpesvirus", "Herpesviridae", "NCBITaxon_3044472"),
]


@pytest.mark.parametrize("umbrella,family,_iri", _UMBRELLAS)
def test_umbrella_extracts_family_name(umbrella, family, _iri):
    # RED before the alias: bare umbrella extracts only itself, which misses.
    assert family in extract_virus_names(umbrella)


@pytest.mark.parametrize(
    "plural,family",
    [
        ("coronaviruses", "Coronaviridae"),
        ("poxviruses", "Poxviridae"),
        ("herpesviruses", "Herpesviridae"),
    ],
)
def test_plural_umbrella_extracts_family(plural, family):
    # locks the (?:es)? branch — the only novel regex element (reviewer A).
    assert family in extract_virus_names(plural)


def test_specific_query_not_collapsed_to_family():
    # over-trigger guard: a specific query must still yield its species, never the family umbrella.
    assert extract_virus_names("SARS-CoV-2")[0] == "Severe acute respiratory syndrome coronavirus 2"
    assert "Coronaviridae" not in extract_virus_names("SARS-CoV-2")


def test_sars_coronavirus_yields_species_before_family():
    # mixed case: "sars coronavirus" matches BOTH a specific pattern and the family alias; specific wins by
    # list order, so the species is first and resolves first (reviewer A).
    names = extract_virus_names("sars coronavirus")
    assert names[0] == "Severe acute respiratory syndrome-related coronavirus"
    assert names.index("Severe acute respiratory syndrome-related coronavirus") < names.index(
        "Coronaviridae"
    )


def test_family_names_resolve_in_dictionary():
    # parity/precondition (GREEN always): the alias targets must actually resolve, else Option B is inert.
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    for _u, family, iri in _UMBRELLAS:
        r = lookup_entity(family)
        assert r.path != "miss", f"{family} should resolve"
        assert iri in (r.canonical_iri or ""), f"{family} -> {r.canonical_iri}"


@pytest.mark.parametrize("umbrella,_family,iri", _UMBRELLAS)
def test_build_resolution_plan_resolves_umbrella(umbrella, _family, iri):
    # end-to-end RED->GREEN: the umbrella query, a miss today, resolves to the family taxon after the alias.
    from apecx_integration.composition.steps.harmonized_resolve_step import build_resolution_plan

    plan = build_resolution_plan(umbrella, "bvbrc_genome")
    assert plan["resolution_path"] != "miss", f"{umbrella} still misses"
    assert iri in (plan["canonical_iri"] or ""), f"{umbrella} -> {plan['canonical_iri']}"


def test_junk_term_stays_a_miss():
    # safety pin: the family alias must not make genuine junk resolve.
    from apecx_integration.composition.steps.harmonized_resolve_step import build_resolution_plan

    plan = build_resolution_plan("totally-made-up-xyzzy", "bvbrc_genome")
    assert plan["resolution_path"] == "miss"
    assert plan["canonical_iri"] is None


@pytest.mark.parametrize("term", ["hemorrhagic fever virus", "hepatitis virus"])
def test_syndrome_seam_recognizes_but_harmonized_search_still_misses(term):
    # HONEST gap (reviewer C): the syndrome seam _syndrome_category DOES recognize these bare terms, but it
    # is wired ONLY into the epitope workflow's TaxonCandidateReviewStep — NOT the harmonized-search pipeline
    # that AB_CONFOUND measured. There these still resolve to a miss and serve raw 0.0-FP records. Option A
    # (a fail-closed diagnosis in the harmonized-search miss envelope) is a tracked follow-up, NOT done here.
    from apecx_integration.composition.steps.harmonized_resolve_step import build_resolution_plan

    assert _syndrome_category(term) is not None  # recognized by the epitope-only seam
    assert (
        build_resolution_plan(term, "bvbrc_genome")["resolution_path"] == "miss"
    )  # harmonized-search gap
