"""Unit tests for the sequence-conservation visualization (helper + AlignmentVizStep).

The text track is the degrade-loud floor (always present); the PNG is best-effort (matplotlib
optional). These pin: text always renders, PNG renders when matplotlib + data are present,
PNG returns None (never raises) on missing data, and the step threads both onto the bundle
without ever raising on a content issue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.composition.steps._alignment_viz import (
    render_conservation_png,
    render_conservation_text,
)
from apecx_integration.composition.steps.alignment_viz_step import AlignmentVizStep

_PER_COLUMN = [{"column": c, "identity": (0.6 + 0.4 * (c % 3 == 0))} for c in range(12)]
_REGIONS = [{"start": 2, "end": 7, "length": 6, "consensus": "MAKLGT", "mean_identity": 0.95}]
_ALN = ">s1\nMAKLGTPQRS--\n>s2\nMAKLGTPQRSAB\n>s3\nMAKLDTPQRS--\n"


def _stage(tmp_path: Path) -> AlignmentVizStep:
    p = tmp_path / "viz.yml"
    p.write_text("name: viz_test\n")
    return AlignmentVizStep.from_config(str(p))


def test_text_track_always_renders_with_consensus_and_sparkline():
    txt = render_conservation_text(_PER_COLUMN, _REGIONS, protein="E1", n_sequences=3)
    assert "MAKLGT" in txt  # consensus motif
    assert "region 1" in txt and "cols 2-7" in txt
    # the sparkline uses block glyphs
    assert any(g in txt for g in "▁▂▃▄▅▆▇█")


def test_text_track_loud_when_no_regions():
    assert "No conserved regions" in render_conservation_text(_PER_COLUMN, [], protein="E1")


def test_png_renders_to_a_file_when_matplotlib_present(tmp_path):
    name = render_conservation_png(
        _PER_COLUMN,
        _REGIONS,
        _ALN,
        protein="E1",
        n_sequences=3,
        dest_dir=tmp_path,
        basename="cons_test",
    )
    # matplotlib is in the dev/viz venv → a PNG basename + a real non-empty file.
    assert name == "cons_test.png"
    out = tmp_path / "cons_test.png"
    assert out.exists() and out.stat().st_size > 0


def test_png_returns_none_on_no_per_column(tmp_path):
    # No per-column data → degrade-loud to None (caller renders the text track), never raise.
    assert render_conservation_png(None, _REGIONS, _ALN, dest_dir=tmp_path) is None
    assert render_conservation_png([], _REGIONS, _ALN, dest_dir=tmp_path) is None


def test_step_threads_text_and_artifact_onto_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_ARTIFACTS_DIR", str(tmp_path))
    bundle = {
        "query": "chikv E1",
        "protein": "E1",
        "taxon_id": 37124,
        "per_column_conservation": _PER_COLUMN,
        "conserved_regions": _REGIONS,
        "alignment_fasta": _ALN,
        "sequence_fetch_summary": {"n_used": 3},
        "stage_reports": [],
    }
    out = asyncio.run(_stage(tmp_path).process(bundle))
    # Text track ALWAYS present (the degrade-loud floor).
    assert "MAKLGT" in out["alignment_viz_text"]
    # PNG artifact recorded (matplotlib present): content-addressed basename
    # (conservation_<taxon>_<protein>_<hash>.png) + the file exists in the artifacts dir.
    art = out["alignment_viz_artifact"]
    assert art.startswith("conservation_37124_E1_") and art.endswith(".png")
    assert (tmp_path / art).exists()
    # A stage report was appended; the bundle passed through.
    assert out["query"] == "chikv E1"
    assert any(r["stage"] == "alignment_viz" for r in out["stage_reports"])


def test_step_degrades_loud_with_no_conservation(tmp_path):
    # No regions/per_column (sequence conservation unavailable) → no raise; text track is the
    # loud "nothing to visualize" line; artifact None.
    bundle = {"query": "q", "stage_reports": []}
    out = asyncio.run(_stage(tmp_path).process(bundle))
    assert out["alignment_viz_artifact"] is None
    assert "No conserved regions" in out["alignment_viz_text"]
    assert out["query"] == "q"


def test_step_raises_only_on_broken_wiring(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(_stage(tmp_path).process(["not", "a", "dict"]))
