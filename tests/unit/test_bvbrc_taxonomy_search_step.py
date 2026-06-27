"""Unit tests for BvbrcTaxonomySearchStep — deterministic BV-BRC taxonomy lookup (fallback step 2).

BV-BRC is mocked by monkeypatching the step's ``_get_json`` (taxonomy rows) and ``_cds_count``
(exact-CDS probe) — no unittest.mock. Real BV-BRC parity for this fallback lives in the
integration suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import requests

from apecx_integration.composition.steps.bvbrc_taxonomy_search_step import (
    _CDS_PROBE_CAP,
    BvbrcTaxonomySearchStep,
)


def _stage(tmp_path: Path, *, max_candidates: int | None = None) -> BvbrcTaxonomySearchStep:
    p = tmp_path / "bvbrc_search.yml"
    body = "name: bvbrc_search_test\n"
    if max_candidates is not None:
        body += f"max_candidates: {max_candidates}\n"
    p.write_text(body)
    return BvbrcTaxonomySearchStep.from_config(str(p))


def _cds_map(step: BvbrcTaxonomySearchStep, mapping: dict[int, int]) -> None:
    """Stub the exact-CDS probe with a taxon_id -> cds-count lookup (missing -> 0)."""
    step._cds_count = lambda tid: mapping.get(tid, 0)  # type: ignore[method-assign]


def test_from_config_constructs(tmp_path):
    step = _stage(tmp_path)
    assert step.COMPONENT_TYPE == "bvbrc_taxonomy_search_step"
    assert step.name == "bvbrc_search_test"


def test_short_circuits_when_already_resolved(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_get_json", lambda *a, **k: pytest.fail("must not hit BV-BRC"))
    monkeypatch.setattr(step, "_cds_count", lambda *a, **k: pytest.fail("must not probe CDS"))
    bundle = {
        "query": "chikv",
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
        "taxon_synonyms": ["Chikungunya virus"],
    }
    out = asyncio.run(step.process({"bvbrc_search_input": bundle}))
    assert "taxon_candidates" not in out


def test_ranks_by_cds_surfacing_covered_clade_over_thin_genus(tmp_path):
    """The coverage-maximizing fix: a genus with the MOST genomes but ~0 exact CDS must rank
    BELOW a descendant clade with fewer genomes but rich CDS (genus Norovirus 0 CDS vs GII 112k)."""
    step = _stage(tmp_path)

    def fake(path, query):
        # genus (most genomes, no exact CDS) + a covered clade (fewer genomes, lots of CDS).
        return [
            {"taxon_id": 142786, "taxon_name": "Norovirus", "genomes": 9000},
            {"taxon_id": 122929, "taxon_name": "Norovirus GII", "genomes": 4000},
        ]

    step._get_json = fake  # type: ignore[method-assign]
    _cds_map(step, {142786: 0, 122929: 112058})
    out = asyncio.run(
        step.process(
            {"bvbrc_search_input": {"query": "norovirus", "taxon_synonyms": ["norovirus"]}}
        )
    )
    cands = out["taxon_candidates"]
    # ranked by cds desc -> the covered clade wins despite fewer genomes.
    assert [c["taxon_id"] for c in cands] == [122929, 142786]
    assert cands[0]["taxon_name"] == "Norovirus GII"
    assert cands[0]["cds"] == 112058
    assert cands[0]["genomes"] == 4000
    # every candidate carries the new genomes + cds fields (hits is gone).
    assert all({"taxon_id", "taxon_name", "genomes", "cds"} <= set(c) for c in cands)
    assert all("hits" not in c for c in cands)


def test_query_constrains_to_viral_division(tmp_path):
    """Pollution fix (2026-06-27): the taxonomy lookup must constrain to the Viruses division, so
    Solr keyword-matching on a short synonym ("HSV") can't surface NON-viral taxa (plants like
    'Radula sp. HSV…', synthetic 'Expression vector …/HSV1 tk', environmental bacteria) into the
    candidate list."""
    step = _stage(tmp_path)
    seen_queries: list[str] = []

    def fake(path, query):
        seen_queries.append(query)
        return [{"taxon_id": 10298, "taxon_name": "Human alphaherpesvirus 1", "genomes": 50}]

    step._get_json = fake  # type: ignore[method-assign]
    _cds_map(step, {10298: 70})
    asyncio.run(step.process({"bvbrc_search_input": {"query": "hsv", "taxon_synonyms": ["HSV"]}}))
    assert seen_queries, "no taxonomy query issued"
    assert all("eq(division,Viruses)" in q for q in seen_queries)


def test_aggregates_and_keeps_max_genomes_name(tmp_path):
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
    _cds_map(step, {})  # all cds 0 -> ranking falls through to genomes desc, taxon_id asc
    out = asyncio.run(
        step.process(
            {"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["Alpha virus", "Beta virus"]}}
        )
    )
    cands = out["taxon_candidates"]
    # cds all 0 -> genomes desc, tie-break taxon_id asc -> 20(100), 30(100), 10(5).
    assert [c["taxon_id"] for c in cands] == [20, 30, 10]
    # taxon 20's name comes from the MAX-genomes row (100), not the stale lower-count row (40).
    assert cands[0]["taxon_name"] == "Alphavirus"
    assert cands[0]["genomes"] == 100


def test_top_k_cap(tmp_path):
    step = _stage(tmp_path, max_candidates=2)

    def fake(path, query):
        return [
            {"taxon_id": 1, "taxon_name": "a", "genomes": 9},
            {"taxon_id": 2, "taxon_name": "b", "genomes": 8},
            {"taxon_id": 3, "taxon_name": "c", "genomes": 7},
        ]

    step._get_json = fake  # type: ignore[method-assign]
    _cds_map(step, {})  # cds 0 -> genomes desc
    out = asyncio.run(step.process({"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["x"]}}))
    cands = out["taxon_candidates"]
    assert len(cands) == 2
    assert [c["taxon_id"] for c in cands] == [1, 2]


def test_cds_probe_cap_respected(tmp_path):
    """Only the top _CDS_PROBE_CAP genome-ranked taxa are CDS-probed; the rest get cds=0 even
    if they have CDS — bounding HTTP cost on a many-synonym run."""
    n = _CDS_PROBE_CAP + 3
    rows = [{"taxon_id": i, "taxon_name": f"t{i}", "genomes": n - i} for i in range(n)]
    step = _stage(tmp_path, max_candidates=n)
    step._get_json = lambda path, query: rows  # type: ignore[method-assign]

    probed: list[int] = []

    def cds(tid: int) -> int:
        probed.append(tid)
        return 1  # any probed taxon reports CDS=1

    step._cds_count = cds  # type: ignore[method-assign]
    out = asyncio.run(step.process({"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["x"]}}))

    # exactly the top-genomes _CDS_PROBE_CAP taxa (genomes desc -> taxon_ids 0.._CAP-1) were probed.
    assert len(probed) == _CDS_PROBE_CAP
    assert set(probed) == set(range(_CDS_PROBE_CAP))
    by_id = {c["taxon_id"]: c for c in out["taxon_candidates"]}
    assert by_id[0]["cds"] == 1  # probed
    assert by_id[n - 1]["cds"] == 0  # beyond the cap -> not probed


def test_per_taxon_cds_error_is_skipped(tmp_path):
    """A CDS probe that raises for one taxon degrades that taxon to cds=0; the run continues."""
    step = _stage(tmp_path)
    step._get_json = lambda path, query: [  # type: ignore[method-assign]
        {"taxon_id": 1, "taxon_name": "boom", "genomes": 10},
        {"taxon_id": 2, "taxon_name": "ok", "genomes": 5},
    ]

    def cds(tid: int) -> int:
        if tid == 1:
            raise requests.HTTPError("503")
        return 99

    step._cds_count = cds  # type: ignore[method-assign]
    out = asyncio.run(step.process({"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["x"]}}))
    by_id = {c["taxon_id"]: c for c in out["taxon_candidates"]}
    assert by_id[1]["cds"] == 0  # errored probe -> 0
    assert by_id[2]["cds"] == 99
    # the covered taxon (2) ranks first despite fewer genomes.
    assert out["taxon_candidates"][0]["taxon_id"] == 2


def test_per_synonym_error_is_skipped(tmp_path):
    step = _stage(tmp_path)

    def fake(path, query):
        if "Bad" in query:
            raise requests.HTTPError("503")
        if "Good" in query:
            return [{"taxon_id": 11, "taxon_name": "Good", "genomes": 3}]
        return []

    step._get_json = fake  # type: ignore[method-assign]
    _cds_map(step, {11: 7})
    out = asyncio.run(
        step.process(
            {"bvbrc_search_input": {"query": "q", "taxon_synonyms": ["Bad virus", "Good virus"]}}
        )
    )
    assert [c["taxon_id"] for c in out["taxon_candidates"]] == [11]


def test_no_synonyms_yields_empty_candidates(tmp_path):
    step = _stage(tmp_path)
    step._get_json = lambda *a, **k: pytest.fail("no synonyms -> no lookups")  # type: ignore[method-assign]
    step._cds_count = lambda *a, **k: pytest.fail("no candidates -> no CDS probe")  # type: ignore[method-assign]
    out = asyncio.run(step.process({"bvbrc_search_input": {"query": "q"}}))
    assert out["taxon_candidates"] == []


def test_non_dict_input_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process("not a dict"))
