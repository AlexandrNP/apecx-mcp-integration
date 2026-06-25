"""Unit tests for the C0 data-readiness stage.

Contracts:

1. COVERAGE — counts each assembled retrieval branch and reports the per-source totals.
2. NAMED GAPS — a branch that returned 0 records is named explicitly (not silently empty),
   so the reader sees the evidence basis is narrower than the full source set.
3. PASSTHROUGH + DEGRADE-LOUD (G127) — the bundle passes through unchanged apart from the
   appended report + ``data_readiness`` key; never raises on a content/shape issue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.data_readiness_step import DataReadinessStep


def _step(tmp_path: Path) -> DataReadinessStep:
    p = tmp_path / "readiness.yml"
    p.write_text("name: readiness_test\n")
    return DataReadinessStep.from_config(str(p))


def _bundle(**over) -> dict:
    b = {
        "query": "chikungunya epitopes",
        "rag_chunks": [{"id": 1}, {"id": 2}],
        "bvbrc_genomes": [{"genome_id": "37124.1"}],
        "violin_mappings": [],
        "publications": [{"doi": "10.x"}],
        "globus_results": [{"subject": "pdb:3N40"}],
    }
    b.update(over)
    return b


def test_loads_via_from_config(tmp_path):
    assert _step(tmp_path).name == "readiness_test"


def test_counts_and_names_gaps(tmp_path):
    step = _step(tmp_path)
    out = asyncio.run(step.process(_bundle()))
    dr = out["data_readiness"]
    assert dr["counts"] == {
        "rag_chunks": 2,
        "bvbrc_genomes": 1,
        "violin_mappings": 0,
        "publications": 1,
        "globus_results": 1,
    }
    assert dr["sources_available"] == 4
    assert dr["total_records"] == 5
    # The empty VIOLIN branch is NAMED as a gap, not silently dropped.
    assert any("VIOLIN" in g for g in dr["gaps"])
    rep = [r for r in out["stage_reports"] if r["stage"] == "data_readiness"][0]
    assert rep["order"] == 0
    assert "Coverage gaps" in rep["markdown"]
    assert "VIOLIN" in rep["markdown"]


def test_passthrough_unchanged(tmp_path):
    """Every assembled key (and the query) survives unchanged; only data_readiness is added."""
    step = _step(tmp_path)
    b = _bundle()
    out = asyncio.run(step.process(b))
    for key in ("query", "rag_chunks", "bvbrc_genomes", "publications", "globus_results"):
        assert out[key] == b[key]


def test_no_gaps_when_all_populated(tmp_path):
    step = _step(tmp_path)
    out = asyncio.run(step.process(_bundle(violin_mappings=[{"synonym_id": "VO_1"}])))
    dr = out["data_readiness"]
    assert dr["gaps"] == []
    assert dr["sources_available"] == 5
    rep = [r for r in out["stage_reports"] if r["stage"] == "data_readiness"][0]
    assert "All retrieval branches returned records" in rep["markdown"]


def test_total_absence_is_named(tmp_path):
    """All branches empty → the report says coverage is NONE (loud), never silent."""
    step = _step(tmp_path)
    empty = {
        "query": "q",
        "rag_chunks": [],
        "bvbrc_genomes": [],
        "violin_mappings": [],
        "publications": [],
        "globus_results": [],
    }
    out = asyncio.run(step.process(empty))
    dr = out["data_readiness"]
    assert dr["sources_available"] == 0
    rep = [r for r in out["stage_reports"] if r["stage"] == "data_readiness"][0]
    assert "NONE" in rep["markdown"]


def test_harmonized_per_index_coverage(tmp_path):
    """Primary path: BV-BRC/VIOLIN now arrive via the Globus destination indices, so coverage
    is counted per-index from harmonized_search_summary.per_index_kept (NOT the dead tabular
    keys). An index with 0 hits is named as a gap with its raw index name."""
    step = _step(tmp_path)
    bundle = {
        "query": "chikungunya epitopes",
        "rag_chunks": [{"id": 1}],
        "publications": [{"doi": "10.x"}],
        "globus_results": [{"subject": "pdb:3N40"}] * 9,
        "harmonized_search_summary": {
            "per_index_kept": {
                "antiviraldb": 2,
                "bvbrc_epitope": 2,
                "bvbrc_genome": 6,
                "bvbrc_protein": 6,
                "bvbrc_protein_structure": 6,
                "protabank": 0,
                "violin_gene": 2,
                "violin_pathogen": 2,
                "violin_vaccine": 3,
            },
            "total_records": 29,
            "map_errors": {},
        },
    }
    out = asyncio.run(step.process(bundle))
    dr = out["data_readiness"]
    # base sources + 9 destination indices = 11 sources; the dead tabular keys are NOT counted.
    assert dr["n_sources"] == 11
    assert "bvbrc_genomes" not in dr["counts"]
    assert dr["counts"]["bvbrc_genome"] == 6
    assert dr["counts"]["protabank"] == 0
    # the empty index is named as a gap with its raw index name.
    assert "no protabank record" in dr["gaps"]
    # one base source (rag/pubmed) + the 8 non-empty indices = 10 populated.
    assert dr["sources_available"] == 10
    rep = [r for r in out["stage_reports"] if r["stage"] == "data_readiness"][0]
    assert "6 bvbrc_genome(s)" in rep["markdown"]
    assert "Coverage gaps" in rep["markdown"]


def test_envelope_unwrap(tmp_path):
    step = _step(tmp_path)
    out = asyncio.run(step.process({"readiness_input": _bundle()}))
    assert out["query"] == "chikungunya epitopes"
    assert out["data_readiness"]["sources_available"] == 4


def test_non_dict_input_raises(tmp_path):
    step = _step(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process("not a dict"))


def test_never_raises_on_missing_keys(tmp_path):
    step = _step(tmp_path)
    out = asyncio.run(step.process({"query": "q"}))
    dr = out["data_readiness"]
    assert dr["sources_available"] == 0
    assert len(dr["gaps"]) == 5


def test_taxon_imprecise_harmonization_disclosed(tmp_path):
    """EF4: when an index's kept count came from the taxon-IMPRECISE raw fallback (per_index_health
    'broken'), data_readiness DISCLOSES it — a clean count must not hide an un-taxon-filtered free-text
    match (e.g. influenza's species-vs-strain taxid mismatch breaks the taxon-IRI leg on ~5 indices)."""
    step = _step(tmp_path)
    bundle = _bundle(
        harmonized_search_summary={
            "per_index_kept": {"protabank": 1, "bvbrc_genome": 10},
            "per_index_health": {"protabank": "broken", "bvbrc_genome": "healthy_parity"},
        }
    )
    dr = asyncio.run(step.process(bundle))["data_readiness"]
    # protabank (broken, non-zero) is disclosed taxon-imprecise; bvbrc_genome (healthy) is NOT
    assert any("protabank" in g and "taxon-IMPRECISE" in g for g in dr["gaps"]), dr["gaps"]
    assert not any("bvbrc_genome" in g and "taxon-IMPRECISE" in g for g in dr["gaps"])
    assert dr["per_index_health"] == {"protabank": "broken", "bvbrc_genome": "healthy_parity"}
