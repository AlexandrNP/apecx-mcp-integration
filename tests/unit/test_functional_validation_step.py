"""Unit tests for the C3 functional-validation stage.

Two contracts:

1. BRUTAL HONESTY — when the assembled evidence carries no residue-level functional
   annotation (the real VIOLIN/BV-BRC shapes), the stage NAMES the absence and states
   the evidence basis ("sequence+structure-derived only"), never fabricates a coincidence.
2. DEGRADE-LOUD (G127) — never raises on a content/shape issue, always passes the bundle
   through with ``functional_validation`` set + a ``functional_validation`` stage report,
   so the chain always reaches ``review``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.functional_validation_step import (
    FunctionalValidationStep,
)


def _step(tmp_path: Path) -> FunctionalValidationStep:
    # fetch_residue_annotations:false keeps these unit tests hermetic (no network) — they
    # cover the legacy VIOLIN/BV-BRC scan + degrade contract; the REAL SIFTS/UniProt/IEDB
    # path has its own network-gated integration tests (test_functional_residue_annotation_*).
    p = tmp_path / "functional.yml"
    p.write_text("name: functional_test\nfetch_residue_annotations: false\n")
    return FunctionalValidationStep.from_config(str(p))


def _bundle(**over) -> dict:
    b = {
        "query": "chikungunya envelope epitopes",
        "conserved_regions": [{"start": 0, "end": 4, "consensus": "MVLEM", "length": 5}],
        "violin_mappings": [{"synonym_id": "VIOLIN_vaccine_1", "source": "VIOLIN_Vaccine"}],
        "bvbrc_genomes": [{"genome_id": "37124.10", "genome_name": "Chikungunya virus"}],
        "structural_reasoning": {
            "available": True,
            "pdb_id": "3N40",
            "exposed_residues": [{"resi": 12}, {"resi": 45}, {"resi": 88}],
        },
    }
    b.update(over)
    return b


def test_loads_via_from_config(tmp_path):
    assert _step(tmp_path).name == "functional_test"


def test_names_absence_of_residue_level_annotation(tmp_path):
    """Real VIOLIN/BV-BRC shapes carry NO residue coordinates → loud honest statement."""
    step = _step(tmp_path)
    out = asyncio.run(step.process(_bundle()))
    fv = out["functional_validation"]
    assert fv["residue_level_annotation_available"] is False
    assert fv["n_candidate_epitope_residues"] == 3
    assert fv["candidate_source"] == "structural_exposed_conserved"
    assert fv["n_immunology_mappings"] == 1
    assert fv["n_genome_features"] == 1
    assert fv["coincidences"] == []
    assert "sequence+structure-derived only" in fv["assessment"]
    rep = [r for r in out["stage_reports"] if r["stage"] == "functional_validation"][0]
    assert rep["order"] == 4
    assert (
        "no residue-level" in rep["markdown"].lower() or "not available" in rep["markdown"].lower()
    )


def test_no_candidates_when_structure_unavailable(tmp_path):
    """Structural reasoning unavailable → no candidate residues; stage still names the basis."""
    step = _step(tmp_path)
    sr = {"available": False, "note": "Containerized PyMOL image not available"}
    out = asyncio.run(step.process(_bundle(structural_reasoning=sr)))
    fv = out["functional_validation"]
    assert fv["n_candidate_epitope_residues"] == 0
    assert fv["candidate_source"] == "conserved_regions_only"
    assert "PyMOL image not available" in fv["assessment"]


def test_detects_coincidence_when_residue_annotation_present(tmp_path):
    """Forward-compat: a record carrying residue positions that overlap candidate residues
    surfaces a REAL coincidence (proves the cross-check works the moment richer data lands)."""
    step = _step(tmp_path)
    violin = [
        {"synonym_id": "EPITOPE_1", "source": "annotated", "position": 45},
        {"synonym_id": "EPITOPE_2", "source": "annotated", "positions": [200, 201]},
    ]
    out = asyncio.run(step.process(_bundle(violin_mappings=violin)))
    fv = out["functional_validation"]
    assert fv["residue_level_annotation_available"] is True
    assert any(c["residue"] == 45 for c in fv["coincidences"])
    assert "COINCIDE" in fv["assessment"]


def test_envelope_unwrap_and_passthrough(tmp_path):
    """Trigger-envelope shape is unwrapped; the bundle (incl. upstream reports) passes through."""
    step = _step(tmp_path)
    inner = _bundle(
        stage_reports=[{"stage": "context_assembly", "order": 1, "markdown": "x", "data": {}}]
    )
    out = asyncio.run(step.process({"functional_input": inner}))
    assert out["query"] == inner["query"]
    stages = {r["stage"] for r in out["stage_reports"]}
    assert {"context_assembly", "functional_validation"} <= stages


def test_non_dict_input_raises(tmp_path):
    step = _step(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process(["not", "a", "dict"]))


def test_never_raises_on_missing_keys(tmp_path):
    """A bundle with none of the expected keys still produces a valid honest result."""
    step = _step(tmp_path)
    out = asyncio.run(step.process({"query": "q"}))
    fv = out["functional_validation"]
    assert fv["candidate_source"] == "none"
    assert fv["n_candidate_epitope_residues"] == 0
    assert isinstance(fv["assessment"], str) and fv["assessment"]
