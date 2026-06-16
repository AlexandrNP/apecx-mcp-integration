"""Unit tests for RheaGenomicAnalysisStep — the RHEA-backed conservation leg.

Focus: the degrade-loud contract (no live RHEA needed). A missing taxon/protein,
or an unreachable RHEA, must produce a NAMED note on the bundle and pass it
through — never raise, never a silent empty result.
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


def test_missing_taxon_degrades_loud(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"query": "chikv", "protein": "E1"}))
    assert out["rhea_conservation"] is None
    assert out["rhea_conservation_note"] and "taxon_id" in out["rhea_conservation_note"]
    # bundle passed through
    assert out["query"] == "chikv"


def test_missing_protein_degrades_loud(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"query": "chikv", "taxon_id": 37124}))
    assert out["rhea_conservation"] is None
    assert "protein" in out["rhea_conservation_note"]


def test_rhea_unreachable_degrades_loud(tmp_path, monkeypatch):
    """A failure inside the RHEA drive becomes a named note, not a raise."""
    step = _stage(tmp_path)

    async def _boom(taxon_id, protein):
        raise RuntimeError("Rhea server unreachable")

    monkeypatch.setattr(step, "_drive_rhea_conservation", _boom)
    out = asyncio.run(step.process({"query": "chikv", "taxon_id": 37124, "protein": "E1"}))
    assert out["rhea_conservation"] is None
    assert "unavailable" in out["rhea_conservation_note"].lower()
    assert "RuntimeError" in out["rhea_conservation_note"]


def test_success_folds_conservation(tmp_path, monkeypatch):
    """A successful RHEA run folds conserved regions into the bundle under rhea_conservation."""
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
    assert out["rhea_conservation_note"] is None


def test_envelope_unwrap(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"rhea_genomic_input": {"query": "x", "taxon_id": 37124}}))
    assert "rhea_conservation_note" in out  # processed the unwrapped bundle


def test_bad_input_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process(["not", "a", "dict"]))
