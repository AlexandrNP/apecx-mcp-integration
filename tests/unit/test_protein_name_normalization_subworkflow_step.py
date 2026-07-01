"""Unit tests for ProteinNameNormalizationSubworkflowStep — the passthrough-degrade nesting seam.

Two properties (no network):
  * degrade-to-ORIGINAL: when the inner cascade fails, the step returns the ORIGINAL ``{taxon_id,
    protein, feature_type}`` payload (so the downstream fetch runs with the un-normalized name) —
    NOT an "unavailable" marker, and NEVER a raise. Normalization is an enhancement; its failure
    must be invisible.
  * G117: the inner first-step input DU (``norm_in``) differs from the step's own input DU
    (``fetch_in``, the conserved-sites entry contract).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nanobrain.library.steps.subworkflow_step import SubworkflowStep

from apecx_integration.composition.steps.protein_name_normalization_subworkflow_step import (
    ProteinNameNormalizationSubworkflowStep,
)


def _stage(tmp_path: Path) -> ProteinNameNormalizationSubworkflowStep:
    p = tmp_path / "normwrap.yml"
    # Constructing builds the inner protein_name_normalization workflow (offline; construction only).
    p.write_text(
        "name: normalize_protein_test\n"
        "input_data_units:\n"
        "  fetch_in:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: fetch_in\n"
    )
    return ProteinNameNormalizationSubworkflowStep.from_config(str(p))


def test_nests_normalization_inner_workflow(tmp_path):
    step = _stage(tmp_path)
    assert step.inner_workflow.name == "protein_name_normalization"
    # G117: the inner first-step input DU (norm_in) MUST differ from this step's own (fetch_in).
    inner_first = next(iter(step.inner_workflow.child_steps.values()))
    assert "norm_in" in inner_first.step_input_data_units
    assert "fetch_in" in step.step_input_data_units
    assert "norm_in" not in step.step_input_data_units


def test_degrade_returns_original_payload_on_inner_failure(tmp_path, monkeypatch):
    step = _stage(tmp_path)

    async def _boom(self, input_data, **kwargs):
        raise RuntimeError("inner cascade blew up")

    # Patch the BASE process so the subclass's super().process(...) raises → the except path runs.
    monkeypatch.setattr(SubworkflowStep, "process", _boom)
    out = asyncio.run(step.process({"fetch_in": {"taxon_id": 11021, "protein": "E2 glycoprotein"}}))
    # ORIGINAL name passed through (NOT an unavailable marker; NOT a raise).
    assert out["taxon_id"] == 11021
    assert out["protein"] == "E2 glycoprotein"
    assert out["original_protein"] == "E2 glycoprotein"
    assert out["match_source"] == "passthrough_error"
