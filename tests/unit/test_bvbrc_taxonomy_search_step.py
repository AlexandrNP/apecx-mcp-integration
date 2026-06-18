"""Unit tests for BvbrcTaxonomySearchStep — deterministic BV-BRC taxonomy lookup (fallback step 2).

BV-BRC is mocked by monkeypatching the step's ``_get_json`` (no unittest.mock). Real BV-BRC
parity for this fallback lives in the integration suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import requests

from apecx_integration.composition.steps.bvbrc_taxonomy_search_step import BvbrcTaxonomySearchStep


def _stage(tmp_path: Path, *, max_candidates: int | None = None) -> BvbrcTaxonomySearchStep:
    p = tmp_path / "bvbrc_search.yml"
    body = "name: bvbrc_search_test\n"
    if max_candidates is not None:
        body += f"max_candidates: {max_candidates}\n"
    p.write_text(body)
    return BvbrcTaxonomySearchStep.from_config(str(p))


def test_from_config_constructs(tmp_path):
    step = _stage(tmp_path)
    assert step.COMPONENT_TYPE == "bvbrc_taxonomy_search_step"
    assert step.name == "bvbrc_search_test"


def test_short_circuits_when_already_resolved(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_get_json", lambda *a, **k: pytest.fail("must not hit BV-BRC"))
    bundle = {
        "query": "chikv",
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
        "taxon_synonyms": ["Chikungunya virus"],
    }
    out = asyncio.run(step.process({"bvbrc_search_input": bundle}))
    assert "taxon_candidates" not in out


def test_ranks_aggregates_and_keeps_max_genomes_name(tmp_path):
    step = _stage(tmp_path)

    def fake(path, query):
        if "Alpha" in query:
            return [
                {"taxon_id": 10, "taxon_name": "Alpha", "genomes": 5},
                {"taxon_id": 20, "taxon_name": "Alphavirus", "genomes": 100},
            ]
        if "Beta" in query:
            return [
                {"taxon_id": 20, "taxon_name": "Alphavirus-stale", "genomes": 40},
                {"taxon_id": 30, "taxon_name": "Beta", "genomes": 100},
            ]
        return []

    step._get_json = fake  # type: ignore[method-assign]
    out = asyncio.run(
        step.process(
            {"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["Alpha virus", "Beta virus"]}}
        )
    )
    cands = out["taxon_candidates"]
    # hits desc, tie-break taxon_id asc -> 20(100), 30(100), 10(5).
    assert [c["taxon_id"] for c in cands] == [20, 30, 10]
    # taxon 20's name comes from the MAX-genomes row (100), not the stale lower-count row (40).
    assert cands[0]["taxon_name"] == "Alphavirus"
    assert cands[0]["hits"] == 100


def test_top_k_cap(tmp_path):
    step = _stage(tmp_path, max_candidates=2)

    def fake(path, query):
        return [
            {"taxon_id": 1, "taxon_name": "a", "genomes": 9},
            {"taxon_id": 2, "taxon_name": "b", "genomes": 8},
            {"taxon_id": 3, "taxon_name": "c", "genomes": 7},
        ]

    step._get_json = fake  # type: ignore[method-assign]
    out = asyncio.run(step.process({"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["x"]}}))
    cands = out["taxon_candidates"]
    assert len(cands) == 2
    assert [c["taxon_id"] for c in cands] == [1, 2]


def test_per_synonym_error_is_skipped(tmp_path):
    step = _stage(tmp_path)

    def fake(path, query):
        if "Bad" in query:
            raise requests.HTTPError("503")
        if "Good" in query:
            return [{"taxon_id": 11, "taxon_name": "Good", "genomes": 3}]
        return []

    step._get_json = fake  # type: ignore[method-assign]
    out = asyncio.run(
        step.process(
            {"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["Bad virus", "Good virus"]}}
        )
    )
    assert [c["taxon_id"] for c in out["taxon_candidates"]] == [11]


def test_no_synonyms_yields_empty_candidates(tmp_path):
    step = _stage(tmp_path)
    step._get_json = lambda *a, **k: pytest.fail("no synonyms -> no lookups")  # type: ignore[method-assign]
    out = asyncio.run(step.process({"bvbrc_search_input": {"query": "q"}}))
    assert out["taxon_candidates"] == []


def test_non_dict_input_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process("not a dict"))
