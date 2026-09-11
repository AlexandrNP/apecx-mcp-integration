"""Smoke tests for literature coverage aggregation (injected abstracts only)."""

from __future__ import annotations

import json

from apecx_integration.agents.literature.coverage import build_coverage, coverage_to_json
from apecx_integration.agents.literature.gazetteer import build_gazetteer

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_DENV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_12637"
_MAP = {
    "chikv": _CHIKV_IRI,
    "chikungunya virus": _CHIKV_IRI,
    "dengue virus": _DENV_IRI,
}


def _by_term(coverage: list[dict]) -> dict[str, dict]:
    return {entry["term"]: entry for entry in coverage}


def test_coverage_counts_chikv_doc_and_omits_fabricated_term():
    gaz = build_gazetteer(_MAP)
    abstracts = [
        {"pmid": "111", "title": "CHIKV in humans", "abstract": "CHIKV spreads."},
        {"pmid": "222", "title": "Dengue review", "abstract": "No arbovirus here."},
    ]

    coverage = build_coverage(abstracts, gaz)
    by_term = _by_term(coverage)

    assert by_term["chikv"]["doc_count"] == 1
    assert by_term["chikv"]["example_pmids"] == ["111"]
    assert by_term["chikv"]["iri"].endswith("NCBITaxon_37124")
    assert "zzz-fabricated-term" not in by_term


def test_deliverable1_artifact_shape_sorted_and_json_roundtrips():
    gaz = build_gazetteer(_MAP)
    abstracts = [
        {"pmid": "111", "title": "CHIKV in humans", "abstract": "CHIKV spreads."},
        {"pmid": "222", "title": "CHIKV again", "abstract": "Chikungunya virus outbreak."},
        {"pmid": "333", "title": "Dengue virus study", "abstract": "Dengue virus is distinct."},
    ]

    coverage = build_coverage(abstracts, gaz)
    by_term = _by_term(coverage)

    # CHIKV mentioned in two distinct documents, with both pmids as examples.
    assert by_term["chikv"]["doc_count"] == 2
    assert by_term["chikv"]["example_pmids"] == ["111", "222"]

    # Sorted by doc_count descending: CHIKV (2) precedes dengue virus (1).
    assert [e["doc_count"] for e in coverage] == sorted(
        (e["doc_count"] for e in coverage), reverse=True
    )
    assert coverage[0]["term"] == "chikv"

    # Every entry carries a valid NCBITaxon PURL and the fabricated term is absent.
    for entry in coverage:
        assert entry["iri"].startswith("http://purl.obolibrary.org/obo/NCBITaxon_")
    assert "zzz-fabricated-term" not in by_term

    # coverage_to_json round-trips back to the same structure.
    assert json.loads(coverage_to_json(coverage)) == coverage
