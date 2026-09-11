"""Shaping + network-gated tests for the bounded PubMed harvester.

The OFFLINE test monkeypatches the Entrez helper and asserts the normalization
contract ({pmid, title, abstract} + abstract-less drop). The NETWORK test runs
the real ``harvest_pubmed`` against public PubMed eUtils (no API key) and skips
when eUtils is unreachable — it is the integration-parity partner of the mock.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from apecx_integration.agents.literature.harvest import harvest_pubmed
from apecx_integration.composition.steps import _pubmed_helpers

# A real eUtils endpoint — the site root returns 403, so probe an actual API path.
_EUTILS_PROBE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"


def _pubmed_reachable() -> bool:
    """Fast connectivity probe: True iff eUtils answers 2xx within a short timeout."""
    try:
        with urllib.request.urlopen(_EUTILS_PROBE, timeout=5) as resp:
            return resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def test_harvest_pubmed_normalizes_and_drops_abstractless(monkeypatch):
    # Arrange: helper returns its rich publication-dict shape; one record has
    # no abstract and must be dropped.
    helper_records = [
        {
            "doi": "10.1/a",
            "title": "Chikungunya E1 epitope mapping",
            "authors": ["Doe, J"],
            "year": "2021",
            "journal": "J Virol",
            "pmid": 33445566,
            "abstract": "We mapped conserved E1 epitopes.",
        },
        {
            "doi": "10.1/b",
            "title": "Alphavirus capsid review",
            "authors": [],
            "year": "2019",
            "journal": "Virology",
            "pmid": "31002003",
            "abstract": "Structural overview of the capsid.",
        },
        {
            "doi": "10.1/c",
            "title": "Title only, no abstract",
            "authors": [],
            "year": "2020",
            "journal": "X",
            "pmid": "99999999",
            "abstract": "",
        },
    ]

    async def _fake_harvest(term, *, max_papers=0):
        return helper_records

    monkeypatch.setattr(_pubmed_helpers, "harvest", _fake_harvest)

    # Act
    records = harvest_pubmed("anything", max_papers=10)

    # Assert: abstract-less dropped; survivors carry exactly the contract keys
    # and a str PMID (the int 33445566 is coerced).
    assert len(records) == 2
    assert all(set(r) == {"pmid", "title", "abstract"} for r in records)
    assert all(isinstance(r["pmid"], str) for r in records)
    assert records[0]["pmid"] == "33445566"
    assert "99999999" not in {r["pmid"] for r in records}


@pytest.mark.slow
@pytest.mark.integration
def test_harvest_pubmed_against_real_eutils():
    if not _pubmed_reachable():
        pytest.skip("PubMed eUtils unreachable — network-gated integration test skipped")

    records = harvest_pubmed("Chikungunya virus", max_papers=5)

    assert len(records) >= 1
    first = records[0]
    assert first["abstract"].strip()
    assert first["pmid"].isdigit()
