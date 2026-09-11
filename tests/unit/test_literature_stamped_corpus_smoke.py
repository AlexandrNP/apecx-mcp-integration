"""Smoke tests for abstract stamping + the in-memory IRI-filterable store."""

from __future__ import annotations

from apecx_integration.agents.literature.gazetteer import build_gazetteer
from apecx_integration.agents.literature.stamped_corpus import (
    StampedCorpus,
    stamp_abstract,
)

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_MAP = {"chikv": _CHIKV_IRI, "chikungunya virus": _CHIKV_IRI}


def test_stamp_and_filter_by_iri():
    gaz = build_gazetteer(_MAP)
    corpus = StampedCorpus()

    chikv_rec = stamp_abstract(
        {"pmid": "1", "title": "Chikungunya virus study", "abstract": "..."}, gaz
    )
    dengue_rec = stamp_abstract(
        {"pmid": "2", "title": "Dengue only", "abstract": "no chik here"}, gaz
    )
    corpus.add(chikv_rec)
    corpus.add(dengue_rec)

    assert _CHIKV_IRI in chikv_rec["iris"]
    assert _CHIKV_IRI not in dengue_rec["iris"]
    assert corpus.filter_by_iri(_CHIKV_IRI) == [chikv_rec]
