"""Unit tests for the virus-name -> (taxon_id, canonical species) resolver.

Name extraction is a pure function (no network). The BV-BRC taxonomy WIRE
(``_query_taxonomy``) is mocked here; the SAME behavior is exercised against the
real BV-BRC index in ``tests/integration/test_taxonomy_resolver_live.py`` (mock /
integration parity rule).
"""

from __future__ import annotations

import pytest

from apecx_integration.agents.globus_search import taxonomy_resolver as tr


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    tr._clear_cache()
    yield
    tr._clear_cache()


# ---- name extraction (pure, no network) ----


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


# ---- resolution (BV-BRC wire mocked) ----


def _fake_rows(*rows):
    return list(rows)


def test_resolve_picks_highest_genome_node(monkeypatch):
    """sort(-genomes) at the wire + our pick-best both keep the canonical species node,
    skipping zero-coverage synthetic/strain nodes."""
    captured = {}

    def fake_wire(name, *, api_base, timeout):
        captured["name"] = name
        return _fake_rows(
            {"taxon_id": "11320", "taxon_name": "Influenza A virus", "genomes": 1834091},
            {"taxon_id": "1522430", "taxon_name": "synthetic Influenza A virus", "genomes": 0},
        )

    monkeypatch.setattr(tr, "_query_taxonomy", fake_wire)
    res = tr.resolve_virus_taxon("Influenza A virus")
    assert res is not None
    assert res.taxon_id == 11320
    assert res.scientific_name == "Influenza A virus"
    assert res.bvbrc_taxon_name == "Influenza A virus"
    assert res.genomes == 1834091
    assert res.source == "bv-brc-taxonomy"
    assert captured["name"] == "Influenza A virus"


def test_resolve_rejects_zero_coverage_only(monkeypatch):
    """A name that resolves only to zero-genome nodes is NOT resolved (no false coverage)."""
    monkeypatch.setattr(
        tr,
        "_query_taxonomy",
        lambda name, *, api_base, timeout: _fake_rows(
            {"taxon_id": "2819200", "taxon_name": "Expression vector SARSCoV2SGFP", "genomes": 0},
        ),
    )
    assert tr.resolve_virus_taxon("nonsense vector name") is None


def test_resolve_empty_result_is_none(monkeypatch):
    monkeypatch.setattr(tr, "_query_taxonomy", lambda name, *, api_base, timeout: [])
    assert tr.resolve_virus_taxon("Unobtainium virus") is None


def test_resolve_network_error_degrades_to_none(monkeypatch):
    """A BV-BRC outage degrades loud (None + WARNING), never raises, never caches the miss."""

    def boom(name, *, api_base, timeout):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(tr, "_query_taxonomy", boom)
    assert tr.resolve_virus_taxon("Influenza A virus") is None
    # Not cached: a later successful call still resolves.
    monkeypatch.setattr(
        tr,
        "_query_taxonomy",
        lambda name, *, api_base, timeout: _fake_rows(
            {"taxon_id": "11320", "taxon_name": "Influenza A virus", "genomes": 5}
        ),
    )
    res = tr.resolve_virus_taxon("Influenza A virus")
    assert res is not None and res.taxon_id == 11320


def test_resolve_is_cached(monkeypatch):
    calls = {"n": 0}

    def counting_wire(name, *, api_base, timeout):
        calls["n"] += 1
        return _fake_rows({"taxon_id": "64320", "taxon_name": "Zika virus", "genomes": 3384})

    monkeypatch.setattr(tr, "_query_taxonomy", counting_wire)
    a = tr.resolve_virus_taxon("Zika virus")
    b = tr.resolve_virus_taxon("Zika virus")
    assert a is b is not None
    assert calls["n"] == 1, "second identical resolve must hit the cache, not the wire"


def test_resolve_query_to_taxon_tries_candidates_in_order(monkeypatch):
    """resolve_query_to_taxon extracts the name then resolves it via the wire."""
    monkeypatch.setattr(
        tr,
        "_query_taxonomy",
        lambda name, *, api_base, timeout: _fake_rows(
            {
                "taxon_id": "2697049",
                "taxon_name": "Severe acute respiratory syndrome coronavirus 2",
                "genomes": 9407630,
            }
        ),
    )
    res = tr.resolve_query_to_taxon("SARS-CoV-2 spike glycoprotein conserved epitopes")
    assert res is not None
    assert res.taxon_id == 2697049
    assert res.scientific_name == "Severe acute respiratory syndrome coronavirus 2"


def test_resolve_query_with_no_virus_name_is_none(monkeypatch):
    monkeypatch.setattr(tr, "_query_taxonomy", lambda *a, **k: pytest.fail("wire must not be hit"))
    assert tr.resolve_query_to_taxon("envelope glycoprotein structure") is None
