"""REAL embedding smoke test for ``build_faiss_subindex``.

Uses the actual all-mpnet-base-v2 model + FAISS (no mocks). ``importorskip``
makes a clean install without the 'rag' extra SKIP rather than error.
"""

import pytest

pytest.importorskip("sentence_transformers")
pytest.importorskip("faiss")

from apecx_integration.agents.literature.stamped_corpus import build_faiss_subindex


def test_antibody_query_ranks_antibody_abstract_first():
    records = [
        {"pmid": 1, "abstract": "a monoclonal antibody that neutralizes the virus in mice"},
        {"pmid": 2, "abstract": "whole genome assembly and phylogenetics of viral isolates"},
        {"pmid": 3, "abstract": "a phase 1 vaccine trial safety and immunogenicity"},
    ]

    index = build_faiss_subindex(records)
    hits = index.search("neutralizing antibody against the virus", k=1)

    assert len(hits) == 1
    assert hits[0]["pmid"] == 1


def test_empty_records_search_returns_empty():
    index = build_faiss_subindex([])

    assert index.search("anything", k=5) == []
