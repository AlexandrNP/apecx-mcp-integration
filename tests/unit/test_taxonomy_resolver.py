"""Unit tests for virus-name extraction from free query text (pure, no network).

Taxon RESOLUTION moved to the dict resolver (``harmonized_resolve_step.build_resolution_plan``);
the live-BV-BRC name-matching resolver this module used to host was retired 2026-06-18 (it
diverged from the dict resolver — no merged_taxons redirect, fuzzy wrong-organism matches). This
module now only extracts candidate names; see ``test_evidence_query_normalize_step.py`` for the
resolution contract.
"""

from __future__ import annotations

import pytest

from apecx_integration.agents.globus_search import taxonomy_resolver as tr


@pytest.mark.parametrize(
    "query,expected_first",
    [
        (
            "SARS-CoV-2 spike glycoprotein conserved epitopes",
            "Severe acute respiratory syndrome coronavirus 2",
        ),
        ("sars cov 2 nucleocapsid", "Severe acute respiratory syndrome coronavirus 2"),
        ("2019-nCoV spike", "Severe acute respiratory syndrome coronavirus 2"),
        ("influenza hemagglutinin stalk epitopes", "Influenza A virus"),
        ("influenza A virus neuraminidase", "Influenza A virus"),
        ("influenza B virus", "Influenza B virus"),
        ("HIV-1 envelope gp120 epitopes", "Human immunodeficiency virus 1"),
        ("HIV broadly neutralizing antibody", "Human immunodeficiency virus 1"),
        ("HIV-2 gp36", "Human immunodeficiency virus 2"),
        ("dengue virus envelope", "Dengue virus"),
        ("chikungunya envelope epitopes", "Chikungunya virus"),
        ("West Nile virus NS1", "West Nile virus"),
        ("Zika prM-E structures", "Zika virus"),
        ("Ebola glycoprotein", "Zaire ebolavirus"),
        ("RSV F protein prefusion", "Human respiratory syncytial virus"),
        ("measles hemagglutinin", "Measles morbillivirus"),
        ("rabies glycoprotein", "Rabies lyssavirus"),
        ("MERS-CoV spike", "Middle East respiratory syndrome-related coronavirus"),
        ("Powassan virus envelope", "Powassan virus"),
        ("Mayaro virus epitopes", "Mayaro virus"),
    ],
)
def test_extract_virus_name_table(query, expected_first):
    names = tr.extract_virus_names(query)
    assert names, f"expected a candidate for {query!r}, got none"
    assert names[0] == expected_first, (query, names)


def test_sars_cov_2_does_not_also_yield_sars_cov_1():
    """The -2 form must not ALSO trigger the SARS-CoV (SARS-1) alias."""
    names = tr.extract_virus_names("SARS-CoV-2 spike epitopes")
    assert names == ["Severe acute respiratory syndrome coronavirus 2"], names


def test_bare_sars_cov_resolves_to_sars1_relatedcorona():
    names = tr.extract_virus_names("SARS-CoV main protease")
    assert names[0] == "Severe acute respiratory syndrome-related coronavirus", names


def test_no_virus_name_extracts_nothing():
    assert tr.extract_virus_names("envelope glycoprotein structure") == []
    assert tr.extract_virus_names("") == []
    assert tr.extract_virus_names("   ") == []
