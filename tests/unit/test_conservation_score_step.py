"""ConservationScoreStep (EO-52) — deterministic per-column conservation.

Hand-built tiny alignments with KNOWN conserved columns; pure computation, no network, no
mocks. These pin the exact identity/region semantics the conserved-sites workflow relies on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.conservation_score_step import ConservationScoreStep

# Columns:   0 1 2 3 4
#            M A K L G
#            M A K L D
#            M A K - G
# → cols 0,1,2 fully conserved (identity 1.0); col3 (L,L,-) identity 2/3 + gap; col4 (G,D,G) 2/3.
_ALN = ">s1\nMAKLG\n>s2\nMAKLD\n>s3\nMAK-G\n"


def _stage(tmp_path: Path, **cfg) -> ConservationScoreStep:
    p = tmp_path / "cons.yml"
    lines = ["name: cons_test"] + [f"{k}: {v}" for k, v in cfg.items()]
    p.write_text("\n".join(lines) + "\n")
    return ConservationScoreStep.from_config(str(p))


def test_conserved_columns_and_region(tmp_path):
    step = _stage(tmp_path)  # default threshold 0.9
    res = asyncio.run(step.process({"alignment_fasta": _ALN}))["conservation_result"]
    assert res["n_sequences"] == 3
    assert res["alignment_length"] == 5
    # Columns 0,1,2 are fully conserved; 3,4 are not (identity 2/3 < 0.9).
    conserved_cols = {s["column"] for s in res["conserved_sites"]}
    assert conserved_cols == {0, 1, 2}
    assert [s["consensus"] for s in res["conserved_sites"]] == ["M", "A", "K"]
    # The three contiguous conserved columns form one region with the consensus motif.
    assert len(res["conserved_regions"]) == 1
    region = res["conserved_regions"][0]
    assert (region["start"], region["end"], region["length"]) == (0, 2, 3)
    assert region["consensus"] == "MAK"


def test_gap_and_identity_math(tmp_path):
    step = _stage(tmp_path)
    cols = asyncio.run(step.process({"alignment_fasta": _ALN}))["conservation_result"]["per_column"]
    # col3: L,L,- → consensus L, identity 2/3, gap_fraction 1/3.
    c3 = cols[3]
    assert c3["consensus"] == "L"
    assert c3["identity"] == pytest.approx(2 / 3, abs=1e-3)
    assert c3["gap_fraction"] == pytest.approx(1 / 3, abs=1e-3)
    assert c3["conserved"] is False
    # col0: fully conserved.
    assert cols[0]["identity"] == 1.0
    assert cols[0]["shannon_bits"] == 0.0
    assert cols[0]["shannon_conservation"] == 1.0


def test_threshold_lowered_marks_more_conserved(tmp_path):
    step = _stage(tmp_path, conservation_threshold=0.6)
    res = asyncio.run(step.process({"alignment_fasta": _ALN}))["conservation_result"]
    # At 0.6, the 2/3-identity columns (3,4) now count as conserved too.
    conserved_cols = {s["column"] for s in res["conserved_sites"]}
    assert conserved_cols == {0, 1, 2, 3, 4}


def test_min_region_length_filters_short_runs(tmp_path):
    # Only column 2 is conserved here; an isolated conserved column is a site but not a region
    # when min_region_length=2.
    aln = ">a\nXYKAB\n>b\nPQKCD\n>c\nLMKEF\n"
    step = _stage(tmp_path, min_region_length=2)
    res = asyncio.run(step.process({"alignment_fasta": aln}))["conservation_result"]
    assert {s["column"] for s in res["conserved_sites"]} == {2}
    assert res["conserved_regions"] == []  # the lone conserved column is below min_region_length


def test_unequal_lengths_fail_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="not aligned|unequal"):
        asyncio.run(step.process({"alignment_fasta": ">a\nMAKL\n>b\nMAK\n"}))


def test_single_sequence_fail_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="≥2|need"):
        asyncio.run(step.process({"alignment_fasta": ">only\nMAKLG\n"}))


def test_empty_input_fail_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="alignment_fasta"):
        asyncio.run(step.process({"alignment_fasta": "   "}))


def test_trigger_envelope_unwrap(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"some_du": {"alignment_fasta": _ALN}}))
    assert out["conservation_result"]["n_sequences"] == 3
