"""Durable per-run artifacts folder + structured output — the deliverables a run must produce.

What this pins:
  1. Each run gathers a self-contained folder ``<run_id>/`` (``_attach_artifact`` → ``artifact_dir``;
     ``artifact_path`` → ``report.md``) so the deliverables survive the LLM / MCP client discarding
     or summarising the tool result.
  2. Figures the report inlines are copied into ``figures/`` (with a vector ``.pdf`` sibling when one
     exists), and inline refs are rewritten to the self-contained ``figures/`` path.
  3. The structured DataShape is split into per-tool native files under ``tool_outputs/`` +
     written whole to ``data.json`` (``collect_structured_output`` → ``data_preview``).
"""

from __future__ import annotations

import json

from apecx_integration.composition.steps.evidence_review_synthesis_step import (
    collect_structured_output,
)
from apecx_integration.mcp_surface.tools.eo_primitives import _attach_artifact


def test_attach_artifact_writes_durable_report_and_data(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_ARTIFACTS_DIR", str(tmp_path))
    result = {
        "markdown": "# Answer\n\nThe report the user must see.",
        "data_preview": {"kind": "bundle", "parts": {"conserved_regions": ["a", "b"]}},
        "run_id": "run-123",
    }
    _attach_artifact(result, "run-123")

    run_dir = tmp_path / "run-123"
    assert result["artifact_dir"] == str(run_dir)
    assert result["artifact_path"] == str(run_dir / "report.md")
    # the FULL report on disk, verbatim (no image refs → unchanged)
    assert (run_dir / "report.md").read_text() == result["markdown"]
    # structured side-car written into the run folder
    assert (run_dir / "data.json").exists()


def test_attach_artifact_gathers_figures_and_splits_tool_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_ARTIFACTS_DIR", str(tmp_path))
    base = tmp_path
    # Figures the report inlines: a matplotlib conservation PNG WITH a vector .pdf sibling, and a
    # PyMOL surface PNG with NO sibling (raster → must stay PNG-only).
    (base / "conservation_37124_E1_abcd.png").write_bytes(b"\x89PNG\r\n")
    (base / "conservation_37124_E1_abcd.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / "2XFB_surface.png").write_bytes(b"\x89PNG\r\n")
    # The raw alignment the composition layer stashed (only its basename rides the handle).
    (base / "alignment_37124_E1_abcd.fasta").write_text(">s1\nMAKL\n")

    md = (
        "# Report\n\n"
        "![Sequence conservation — E1](conservation_37124_E1_abcd.png)\n\n"
        "![Epitope surface map — 2XFB](2XFB_surface.png)\n\n"
        "![external](https://example.org/x.png)\n"
    )
    result = {
        "markdown": md,
        "data_preview": {
            "kind": "bundle",
            "parts": {
                "conserved_regions": [{"start": 1, "end": 9}],
                "structural_records": [{"subject": "pdb:2XFB", "n_exposed": 3}],
                "structural_reasoning": {"available": True, "pdb_id": "2XFB"},
                "publications": [{"doi": "10.1/x", "title": "T"}],
                "alignment_fasta_artifact": "alignment_37124_E1_abcd.fasta",
            },
        },
        "run_id": "run-9",
    }
    _attach_artifact(result, "run-9")

    run_dir = tmp_path / "run-9"
    figs = run_dir / "figures"
    tools = run_dir / "tool_outputs"
    # Figures copied; the matplotlib one brought its vector PDF, the PyMOL one stayed PNG-only.
    assert (figs / "conservation_37124_E1_abcd.png").exists()
    assert (figs / "conservation_37124_E1_abcd.pdf").exists()
    assert (figs / "2XFB_surface.png").exists()
    assert not (figs / "2XFB_surface.pdf").exists()
    # Inline refs rewritten to the self-contained figures/ path; the external URL is untouched.
    report = (run_dir / "report.md").read_text()
    assert "](figures/conservation_37124_E1_abcd.png)" in report
    assert "](figures/2XFB_surface.png)" in report
    assert "](https://example.org/x.png)" in report
    # Split-per-tool native files.
    assert json.loads((tools / "conserved_regions.json").read_text()) == [{"start": 1, "end": 9}]
    sasa = json.loads((tools / "structural_sasa.json").read_text())
    assert sasa["structural_records"][0]["subject"] == "pdb:2XFB"
    assert sasa["structural_reasoning"]["pdb_id"] == "2XFB"
    assert json.loads((tools / "publications.json").read_text())[0]["doi"] == "10.1/x"
    assert (tools / "alignment.fasta").read_text() == ">s1\nMAKL\n"


def test_attach_artifact_never_raises_on_empty_or_bad_input(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_ARTIFACTS_DIR", str(tmp_path))
    # no markdown → no artifact, no crash, no artifact_path key
    r1: dict = {"markdown": "", "run_id": "x"}
    _attach_artifact(r1, "x")
    assert "artifact_path" not in r1
    # no run_id → skip
    r2: dict = {"markdown": "# A"}
    _attach_artifact(r2, None)
    assert "artifact_path" not in r2


def test_collect_structured_output_carries_the_real_analysis():
    bundle = {
        "query": "chikv E1 epitopes",
        "taxon_id": 37124,
        "protein": "E1",
        "conserved_regions": [{"start": 1, "end": 9, "identity": 0.95}],
        "structural_records": [{"subject": "pdb:2XFB"}, {"subject": "emdb:EMD-1"}],
        "publications": [{"doi": "10.1/x", "title": "T", "year": 2024}],
    }
    data = collect_structured_output(bundle)
    assert data["kind"] == "bundle"
    parts = data["parts"]
    assert parts["taxon_id"] == 37124 and parts["protein"] == "E1"
    assert parts["counts"] == {"conserved_regions": 1, "structural_records": 2, "publications": 1}
    assert parts["publications"][0]["doi"] == "10.1/x"
    # it serialises cleanly (it lands in a .json artifact)
    json.dumps(data, default=str)
