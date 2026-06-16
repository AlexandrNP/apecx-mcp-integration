"""Unit tests for EvidenceDistillationStep — the deterministic top-N digest leg.

Focus: the throughput contract. Retrieval upstream is unbounded; this step ranks
the full corpus by a DETERMINISTIC quality score and REPLACES each source list
with its top-N (the working set for the LLM + Sources ledger), recording the
pre-truncation totals so coverage stays honest.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.evidence_distillation_step import (
    EvidenceDistillationStep,
)


def _stage(tmp_path: Path, **cfg) -> EvidenceDistillationStep:
    p = tmp_path / "distill.yml"
    body = "name: distill_test\n"
    for k, v in cfg.items():
        body += f"{k}: {v}\n"
    p.write_text(body)
    return EvidenceDistillationStep.from_config(str(p))


def _pub(doi: str, *, title: str = "", abstract: str = "", year: int | None = None) -> dict:
    return {"doi": doi, "title": title, "abstract": abstract, "year": year}


def _bundle(**over) -> dict:
    b = {"query": "chikungunya structural polyprotein epitope"}
    b.update(over)
    return b


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path, max_publications=3)
    assert step.name == "distill_test"
    assert step._max_publications == 3


def test_replaces_source_with_top_n_and_records_total(tmp_path):
    step = _stage(tmp_path, max_publications=2)
    pubs = [_pub(f"10.{i}", year=2000 + i) for i in range(5)]
    out = asyncio.run(step.process(_bundle(publications=pubs)))
    # The working list is REPLACED by the top-N (the digest flows downstream)...
    assert len(out["publications"]) == 2
    # ...and the pre-truncation total is recorded for honest coverage.
    assert out["source_totals"]["publications"] == 5


def test_ranks_by_quality_richest_first(tmp_path):
    """A record with abstract + DOI + recency + term-overlap outranks a bare one."""
    step = _stage(tmp_path, max_publications=1)
    rich = _pub(
        "10.rich",
        title="Chikungunya structural polyprotein epitope mapping",
        abstract="conserved epitope on the structural polyprotein",
        year=2022,
    )
    bare = _pub("10.bare", title="unrelated", year=2001)
    out = asyncio.run(step.process(_bundle(publications=[bare, rich])))
    assert out["publications"][0]["doi"] == "10.rich"


def test_deterministic_across_runs(tmp_path):
    step = _stage(tmp_path, max_publications=3)
    pubs = [_pub(f"10.{i}", title="chikungunya", year=2010) for i in range(6)]
    first = asyncio.run(step.process(_bundle(publications=list(pubs))))
    second = asyncio.run(step.process(_bundle(publications=list(reversed(pubs)))))
    # Same set, different input order -> identical ranked digest (stable tiebreaker).
    assert [p["doi"] for p in first["publications"]] == [p["doi"] for p in second["publications"]]


def test_structural_records_get_a_bonus(tmp_path):
    """A pdb:/emdb: Globus record outranks a non-structural one with equal overlap."""
    step = _stage(tmp_path, max_globus_results=1)
    structural = {"subject": "pdb:2XFB", "title": "chikungunya structure"}
    other = {"subject": "datacite:abc", "title": "chikungunya dataset"}
    out = asyncio.run(step.process(_bundle(globus_results=[other, structural])))
    assert out["globus_results"][0]["subject"] == "pdb:2XFB"


def test_structural_records_list_is_also_capped(tmp_path):
    """The separate structural_records list (the Structural section source) is digested too."""
    step = _stage(tmp_path, max_structural_records=3)
    recs = [{"subject": f"pdb:{i}", "title": "chikungunya structure"} for i in range(10)]
    out = asyncio.run(step.process(_bundle(structural_records=recs)))
    assert len(out["structural_records"]) == 3
    assert out["source_totals"]["structural_records"] == 10


def test_missing_sources_degrade_loud_not_crash(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process(_bundle()))  # no source keys at all
    assert out["publications"] == []
    assert out["globus_results"] == []
    assert out["bvbrc_genomes"] == []
    assert out["violin_mappings"] == []
    assert out["source_totals"]["publications"] == 0
    assert "distillation_note" in out


def test_non_dict_entries_dropped(tmp_path):
    step = _stage(tmp_path, max_publications=5)
    out = asyncio.run(step.process(_bundle(publications=[_pub("10.ok"), "junk", None, 42])))
    assert [p["doi"] for p in out["publications"]] == ["10.ok"]


def test_envelope_unwrap(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"distill_input": _bundle(publications=[_pub("10.x")])}))
    assert out["publications"][0]["doi"] == "10.x"


def test_stage_report_appended(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process(_bundle(publications=[_pub("10.x")])))
    reports = out.get("stage_reports") or []
    assert any(r.get("stage") == "evidence_distillation" for r in reports)


def test_bad_input_shape_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process(["not", "a", "dict"]))
    with pytest.raises(ValueError):
        asyncio.run(step.process({"publications": []}))  # missing query
