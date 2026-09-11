"""Smoke tests for literature coverage aggregation (injected abstracts only)."""

from __future__ import annotations

from apecx_integration.agents.literature.coverage import build_coverage
from apecx_integration.agents.literature.gazetteer import build_gazetteer

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_MAP = {"chikv": _CHIKV_IRI, "chikungunya virus": _CHIKV_IRI}


def test_coverage_counts_chikv_doc_and_omits_fabricated_term():
    gaz = build_gazetteer(_MAP)
    abstracts = [
        {"pmid": "111", "title": "CHIKV in humans", "abstract": "CHIKV spreads."},
        {"pmid": "222", "title": "Dengue review", "abstract": "No arbovirus here."},
    ]

    coverage = build_coverage(abstracts, gaz)

    assert coverage["chikv"]["doc_count"] == 1
    assert coverage["chikv"]["example_pmids"] == ["111"]
    assert coverage["chikv"]["iri"].endswith("NCBITaxon_37124")
    assert "zzz-fabricated-term" not in coverage
