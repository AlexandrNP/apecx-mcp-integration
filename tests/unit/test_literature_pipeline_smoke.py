"""Shaping + network/dict-gated tests for the harvest→stamp pipeline glue.

The OFFLINE test monkeypatches ``harvest_pubmed`` and an injected gazetteer to
assert the stamping wiring (the CHIKV abstract carries the CHIKV IRI, the other
does not). The gated test is the integration-parity partner: it builds a real
bounded gazetteer from dictionary.sqlite and harvests real PubMed.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from apecx_integration.agents.literature import pipeline
from apecx_integration.agents.literature.gazetteer import build_from_dictionary, build_gazetteer
from apecx_integration.agents.literature.pipeline import harvest_and_stamp

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_REAL_DB = "/Users/onarykov/Downloads/apecx-cowork/dictionary.sqlite"
_EUTILS_PROBE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"


def _pubmed_reachable() -> bool:
    try:
        with urllib.request.urlopen(_EUTILS_PROBE, timeout=5) as resp:
            return resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def test_harvest_and_stamp_stamps_only_matching_organism(monkeypatch):
    # Arrange: harvest returns two abstracts, one CHIKV and one unrelated.
    fake_records = [
        {"pmid": "1", "title": "CHIKV study", "abstract": "Chikungunya virus E1 epitopes."},
        {"pmid": "2", "title": "Capsid review", "abstract": "General alphavirus capsid."},
    ]
    monkeypatch.setattr(pipeline, "harvest_pubmed", lambda term, *, max_papers: fake_records)
    gaz = build_gazetteer({"chikungunya virus": _CHIKV_IRI})

    # Act
    records = harvest_and_stamp("Chikungunya virus", gaz, max_papers=10)

    # Assert
    assert {r["pmid"]: r["iris"] for r in records} == {"1": [_CHIKV_IRI], "2": []}


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(not os.path.exists(_REAL_DB), reason="real dictionary.sqlite not present")
def test_harvest_and_stamp_against_real_pubmed_and_dict():
    if not _pubmed_reachable():
        pytest.skip("PubMed eUtils unreachable — network-gated integration test skipped")

    # Bounded but large enough to include CHIKV synonyms in the shipped dict's
    # row order (the exact "chikungunya virus" surface sits ~row 275k).
    gaz = build_from_dictionary(_REAL_DB, limit=300_000)
    assert gaz.tag("Chikungunya virus"), "bounded gazetteer missed CHIKV — raise the limit"

    records = harvest_and_stamp("Chikungunya virus", gaz, max_papers=3)

    assert any(_CHIKV_IRI in r["iris"] for r in records)
