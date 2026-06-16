"""Durable markdown artifact + structured output — the deliverables the workflow must produce.

Two requirements this pins:
  1. The MARKDOWN report is written to a DURABLE file so it survives the LLM / MCP client
     discarding or summarising the tool result (``_attach_artifact`` → ``artifact_path``).
  2. The workflow emits STRUCTURED data alongside the prose (``collect_structured_output``),
     written to the run's ``.json`` artifact and surfaced as ``data_preview``.
"""

from __future__ import annotations

import json

from apecx_integration.composition.steps.evidence_review_synthesis_step import (
    collect_structured_output,
)
from apecx_integration.mcp_surface.tools.eo_primitives import _attach_artifact


def test_attach_artifact_writes_durable_md_and_json(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_ARTIFACTS_DIR", str(tmp_path))
    result = {
        "markdown": "# Answer\n\nThe report the user must see.",
        "data_preview": {"kind": "bundle", "parts": ["a", "b"]},
        "run_id": "run-123",
    }
    _attach_artifact(result, "run-123")

    md_path = tmp_path / "run-123.md"
    assert result["artifact_path"] == str(md_path)
    assert md_path.read_text() == result["markdown"]  # the FULL report on disk, verbatim
    # structured side-car written too
    assert (tmp_path / "run-123.json").exists()


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
