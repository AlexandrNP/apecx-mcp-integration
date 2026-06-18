"""Unit tests for RheaGenomicAnalysisStep — the MANDATORY-but-degrade-loud RHEA conservation leg.

RHEA genomic-analysis is a mandatory PART of the analysis (always attempted + DISCLOSED), but its
absence DEGRADES LOUD — it does NOT fail the run. A missing taxon/protein or a RHEA runtime failure
produces a prominent warning + `apecx-setup rhea` fix instructions (a named note + a proceed_notes
entry) and the bundle passes through so the rest of the analysis still completes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.rhea_genomic_analysis_step import (
    RheaGenomicAnalysisStep,
)


def _stage(tmp_path: Path, **cfg) -> RheaGenomicAnalysisStep:
    p = tmp_path / "rhea_genomic.yml"
    body = "name: rhea_genomic_test\n" + "".join(f"{k}: {v}\n" for k, v in cfg.items())
    p.write_text(body)
    return RheaGenomicAnalysisStep.from_config(str(p))


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path, timeout_seconds=120)
    assert step.name == "rhea_genomic_test"
    assert step._timeout == 120.0


def _assert_loud_unavailable(out: dict) -> None:
    note = out["rhea_conservation_note"]
    assert note and "not available" in note.lower(), note
    assert "apecx-setup rhea" in note, note  # fix instructions
    assert "still" in note.lower() or "remains valid" in note.lower(), note  # don't-fail framing
    # a loud "how to proceed" entry is appended for prominence
    pn = out.get("proceed_notes") or []
    assert any("rhea" in (n.get("stage", "") + n.get("what", "")).lower() for n in pn), pn


def test_missing_taxon_degrades_loud(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"query": "chikv", "protein": "E1"}))
    assert out["rhea_conservation"] is None
    assert out["query"] == "chikv"  # bundle passed through (did not fail)
    _assert_loud_unavailable(out)


def test_missing_protein_degrades_loud(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"query": "chikv", "taxon_id": 37124}))
    assert out["rhea_conservation"] is None
    _assert_loud_unavailable(out)


def test_rhea_failure_degrades_loud_not_raises(tmp_path, monkeypatch):
    """A failure inside the RHEA drive becomes a loud warning + fix instructions, NOT a raise."""
    step = _stage(tmp_path)

    async def _boom(taxon_id, protein):
        raise RuntimeError("Rhea server unreachable")

    monkeypatch.setattr(step, "_drive_rhea_conservation", _boom)
    out = asyncio.run(step.process({"query": "chikv", "taxon_id": 37124, "protein": "E1"}))
    assert out["rhea_conservation"] is None
    assert "RuntimeError" in out["rhea_conservation_note"]
    _assert_loud_unavailable(out)


def test_success_folds_conservation(tmp_path, monkeypatch):
    step = _stage(tmp_path)

    async def _ok(taxon_id, protein):
        return {
            "markdown": "# Conserved sites\n...",
            "data": {
                "parts": {
                    "conservation_result": {
                        "conserved_regions": [
                            {"start": 98, "end": 209},
                            {"start": 226, "end": 315},
                        ],
                        "n_sequences": 60,
                        "alignment_length": 439,
                    }
                }
            },
        }

    monkeypatch.setattr(step, "_drive_rhea_conservation", _ok)
    out = asyncio.run(step.process({"query": "chikv", "taxon_id": 37124, "protein": "E1"}))
    rc = out["rhea_conservation"]
    assert rc["n_sequences"] == 60
    assert rc["alignment_length"] == 439
    assert len(rc["conserved_regions"]) == 2
    assert rc["aligner"] == "muscle"
    assert out["rhea_conservation_note"] is None  # no warning on success


def test_envelope_unwrap(tmp_path):
    out = asyncio.run(
        _stage(tmp_path).process({"rhea_genomic_input": {"query": "x", "taxon_id": 37124}})
    )
    assert "rhea_conservation_note" in out  # processed the unwrapped bundle (degraded: no protein)


def test_bad_input_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process(["not", "a", "dict"]))
