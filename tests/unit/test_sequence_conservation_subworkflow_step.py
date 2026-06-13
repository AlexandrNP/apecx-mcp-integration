"""Unit tests for SequenceConservationSubworkflowStep — the degrade-loud nesting seam (no network).

These cover the FAST-degrade pre-check only (a query with no usable taxon_id/protein): it must
return the named ``sequence_conservation_unavailable`` marker WITHOUT invoking the inner cascade
(so no BV-BRC / MAFFT call, no 180s timeout). The happy path needs real BV-BRC + MAFFT and is
covered by the gated integration test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.composition.steps.sequence_conservation_subworkflow_step import (
    UNAVAILABLE_KEY,
    SequenceConservationSubworkflowStep,
)


def _stage(tmp_path: Path) -> SequenceConservationSubworkflowStep:
    p = tmp_path / "sequence.yml"
    # Constructing builds the inner viral_conserved_sites workflow (offline; construction only).
    p.write_text(
        "name: sequence_test\n"
        "input_data_units:\n"
        "  sequence_params:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: sequence_params\n"
    )
    return SequenceConservationSubworkflowStep.from_config(str(p))


def test_nests_conserved_sites_inner_workflow(tmp_path):
    step = _stage(tmp_path)
    assert step.inner_workflow.name == "viral_conserved_sites"
    # G117: the inner first-step input DU is fetch_in, which must differ from this step's own.
    inner_first = next(iter(step.inner_workflow.child_steps.values()))
    assert "fetch_in" in inner_first.step_input_data_units


def test_fast_degrade_when_no_taxon(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"query": "chikungunya E1", "protein": "E1"}))
    assert list(out) == [UNAVAILABLE_KEY]
    assert "taxon_id" in out[UNAVAILABLE_KEY]


def test_fast_degrade_when_no_protein(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"query": "chikungunya", "taxon_id": 37124}))
    assert list(out) == [UNAVAILABLE_KEY]
    assert "protein" in out[UNAVAILABLE_KEY]


def test_fast_degrade_unwraps_trigger_envelope(tmp_path):
    # The framework delivers {<my_input_du>: payload}; the pre-check must unwrap it.
    out = asyncio.run(_stage(tmp_path).process({"sequence_params": {"protein": "E1"}}))
    assert list(out) == [UNAVAILABLE_KEY]
    assert "taxon_id" in out[UNAVAILABLE_KEY]
