"""Unit tests for RheaGenomicAnalysisStep — the MANDATORY RHEA-backed conservation leg.

Focus: the fail-closed contract (no live RHEA needed). RHEA is mandatory — a missing
taxon/protein, or a RHEA runtime failure, must RAISE (no degrade-to-note, no silent empty,
no MAFFT-only fallback). The prereq gate (catalog ``requires: rhea``) refuses the run up
front when RHEA is not configured; this step fails loud when the RHEA call itself fails.
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


def test_unusable_params_raise_missing_taxon(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="MANDATORY RHEA"):
        asyncio.run(step.process({"query": "chikv", "protein": "E1"}))


def test_unusable_params_raise_missing_protein(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="MANDATORY RHEA"):
        asyncio.run(step.process({"query": "chikv", "taxon_id": 37124}))


def test_rhea_failure_raises_not_degrades(tmp_path, monkeypatch):
    """A failure inside the RHEA drive RAISES (no degrade-to-note swallow)."""
    step = _stage(tmp_path)

    async def _boom(taxon_id, protein):
        raise RuntimeError("Rhea server unreachable")

    monkeypatch.setattr(step, "_drive_rhea_conservation", _boom)
    with pytest.raises(RuntimeError, match="MANDATORY RHEA genomic-analysis"):
        asyncio.run(step.process({"query": "chikv", "taxon_id": 37124, "protein": "E1"}))


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
    """The step unwraps the single-key trigger envelope before reading params — proven by the
    MANDATORY-leg error naming the inner missing field (protein)."""
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="protein"):
        asyncio.run(step.process({"rhea_genomic_input": {"query": "x", "taxon_id": 37124}}))


def test_bad_input_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process(["not", "a", "dict"]))
