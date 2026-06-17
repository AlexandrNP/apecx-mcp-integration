"""Unit tests for per-clade grouping + broad-effectiveness breadth (Req 5).

Synthetic 2-clade alignment: a shared conserved CORE (cols 0-9, identical in both clades) and a
clade-SPECIFIC tail (cols 10-19, identical within each clade but different between them). The
breadth analysis must call the core PAN-CLADE (broad-spectrum) and the tail CLADE-RESTRICTED.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.composition.steps._clade_grouping import (
    clade_conservation_breadth,
    cluster_by_identity,
    pairwise_identity,
)
from apecx_integration.composition.steps.clade_grouping_step import CladeGroupingStep
from apecx_integration.composition.steps.cross_clade_aggregate_step import CrossCladeAggregateStep

# cols 0-9 shared core (MKLGTPQRST); cols 10-19 clade-specific (A-run vs C-run).
_A = "MKLGTPQRSTAAAAAAAAAA"
_C = "MKLGTPQRSTCCCCCCCCCC"
_ALIGNED = [("a1", _A), ("a2", _A), ("a3", _A), ("b1", _C), ("b2", _C), ("b3", _C)]
_FASTA = "".join(f">{i}\n{s}\n" for i, s in _ALIGNED)


def test_pairwise_identity_ignores_gaps():
    assert pairwise_identity("MK-LG", "MK-LG") == 1.0
    assert pairwise_identity(_A, _C) == 0.5  # 10/20 columns match


def test_cluster_splits_divergent_and_merges_homogeneous():
    res = cluster_by_identity(_ALIGNED, threshold=0.95, min_size=2)
    assert len(res["clades"]) == 2
    assert {frozenset(c) for c in res["clades"]} == {
        frozenset(["a1", "a2", "a3"]),
        frozenset(["b1", "b2", "b3"]),
    }
    # homogeneous → 1 clade
    homo = cluster_by_identity([("x", _A), ("y", _A)], threshold=0.95, min_size=2)
    assert len(homo["clades"]) == 1
    # a lone outlier is ungrouped, never silently dropped
    out = cluster_by_identity(
        [("a1", _A), ("a2", _A), ("z", "WWWWWWWWWWWWWWWWWWWW")], threshold=0.95, min_size=2
    )
    assert out["clades"] == [["a1", "a2"]] and out["ungrouped"] == ["z"]


def test_breadth_distinguishes_pan_clade_from_clade_restricted():
    b = clade_conservation_breadth(
        _ALIGNED, [["a1", "a2", "a3"], ["b1", "b2", "b3"]], identity_threshold=0.9, min_region=3
    )
    assert b["available"] and b["n_clades"] == 2
    # the shared core (cols 0-9) is pan-clade
    pan = b["pan_clade_regions"]
    assert any(r["start"] == 0 and r["end"] == 9 and "MKLGTPQRST" in r["consensus"] for r in pan)
    # the divergent tail (cols 10-19) is clade-restricted (conserved within each clade, differs)
    restr = b["clade_restricted_regions"]
    assert any(r["start"] == 10 and r["end"] == 19 for r in restr)
    # the clade-restricted region records the DIFFERING per-clade consensus
    tail = next(r for r in restr if r["start"] == 10)
    assert tail["per_clade_consensus"][0] != tail["per_clade_consensus"][1]


def test_breadth_not_applicable_under_two_clades():
    b = clade_conservation_breadth(_ALIGNED, [["a1", "a2", "a3"]], identity_threshold=0.9)
    assert b["available"] is False


def _grouping_stage(tmp_path: Path) -> CladeGroupingStep:
    p = tmp_path / "cg.yml"
    p.write_text("name: cg_test\n")
    return CladeGroupingStep.from_config(str(p))


def _aggregate_stage(tmp_path: Path) -> CrossCladeAggregateStep:
    p = tmp_path / "cc.yml"
    p.write_text("name: cc_test\n")
    return CrossCladeAggregateStep.from_config(str(p))


def test_clade_grouping_step_emits_groups_and_fastas(tmp_path):
    records = [{"id": i, "sequence": s.replace("-", "")} for i, s in _ALIGNED]
    bundle = {
        "query": "q",
        "protein": "E1",
        "alignment_fasta": _FASTA,
        "sequence_used_records": records,
    }
    out = asyncio.run(_grouping_stage(tmp_path).process(bundle))
    assert len(out["clade_groups"]) == 2
    assert len(out["clade_fastas"]) == 2
    assert out["clade_fastas"][0].count(">") == 3  # 3 members in clade 0
    assert out["query"] == "q"  # passthrough


def test_clade_grouping_step_degrades_loud_when_homogeneous(tmp_path):
    homo_fasta = "".join(f">{i}\n{_A}\n" for i in ("x", "y", "z"))
    records = [{"id": i, "sequence": _A} for i in ("x", "y", "z")]
    bundle = {"query": "q", "alignment_fasta": homo_fasta, "sequence_used_records": records}
    out = asyncio.run(_grouping_stage(tmp_path).process(bundle))
    assert out["clade_groups"] == [] and out["clade_fastas"] == []
    assert "homogeneous" in out["clade_grouping"]["note"]


def test_cross_clade_step_computes_breadth(tmp_path):
    bundle = {
        "query": "q",
        "alignment_fasta": _FASTA,
        "clade_groups": [
            {"clade_id": 0, "member_ids": ["a1", "a2", "a3"], "size": 3},
            {"clade_id": 1, "member_ids": ["b1", "b2", "b3"], "size": 3},
        ],
    }
    out = asyncio.run(_aggregate_stage(tmp_path).process(bundle))
    b = out["cross_clade_breadth"]
    assert (
        b["available"]
        and len(b["pan_clade_regions"]) >= 1
        and len(b["clade_restricted_regions"]) >= 1
    )


def test_cross_clade_step_not_applicable_single_clade(tmp_path):
    bundle = {
        "query": "q",
        "alignment_fasta": _FASTA,
        "clade_groups": [],
        "clade_grouping": {"note": "homogeneous"},
    }
    out = asyncio.run(_aggregate_stage(tmp_path).process(bundle))
    assert out["cross_clade_breadth"]["available"] is False
