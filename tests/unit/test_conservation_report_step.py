"""ConservationReportStep (EO-53) — markdown + Bundle rendering of a conservation result."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.conservation_report_step import ConservationReportStep

_CONS = {
    "n_sequences": 4,
    "alignment_length": 6,
    "conservation_threshold": 0.9,
    "mean_identity": 0.83,
    "n_conserved_columns": 3,
    "conserved_sites": [
        {"column": 0, "consensus": "M", "identity": 1.0},
        {"column": 1, "consensus": "A", "identity": 1.0},
        {"column": 2, "consensus": "K", "identity": 1.0},
    ],
    "conserved_regions": [
        {"start": 0, "end": 2, "length": 3, "consensus": "MAK", "mean_identity": 1.0}
    ],
}


def _stage(tmp_path: Path, **cfg) -> ConservationReportStep:
    p = tmp_path / "report.yml"
    lines = ["name: report_test"] + [f"{k}: {v}" for k, v in cfg.items()]
    p.write_text("\n".join(lines) + "\n")
    return ConservationReportStep.from_config(str(p))


def test_renders_markdown_and_bundle(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process(_CONS))["report"]
    assert "Conserved sites" in out["markdown"]
    assert "4" in out["markdown"]  # n_sequences
    assert "MAK" in out["markdown"]  # the conserved motif
    # The structured result is carried as a Bundle for the EnvelopeStep to stash behind a handle.
    assert out["data"]["kind"] == "bundle"
    assert out["data"]["parts"] == _CONS


def test_no_regions_message(tmp_path):
    step = _stage(tmp_path)
    cons = {**_CONS, "conserved_regions": [], "conserved_sites": []}
    out = asyncio.run(step.process(cons))["report"]
    assert "No region" in out["markdown"]


def test_not_a_conservation_result_fails_loud(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="not a conservation result|alignment_length"):
        asyncio.run(step.process({"something": "else"}))


def test_trigger_envelope_unwrap(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"report_input": _CONS}))["report"]
    assert "Conserved sites" in out["markdown"]
