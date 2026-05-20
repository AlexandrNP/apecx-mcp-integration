"""Integration test (EO-40/41/42): G4 provenance + G37 events captured around a real
workflow run, folded into a run summary.

Critically asserts that provenance auto-capture ACTUALLY FIRES (non-empty records) — a
context that activates but records nothing would be a silent failure: a "what ran" view
that's always empty while tests trivially pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.runtime.provenance_wiring import (
    run_with_provenance,
    summarize_run,
)

ENVELOPE_STEP_CLASS = "apecx_integration.composition.steps.envelope_step.EnvelopeStep"
_WRAPPER = str(
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "steps"
    / "envelope_step.yml"
)


def _build_envelope_workflow(name: str):
    from nanobrain.lightweight import WorkflowBuilder

    builder = WorkflowBuilder(name=name, description="Provenance demo (programmatic build).")
    builder.add_step("envelope", ENVELOPE_STEP_CLASS, description="wrap output", config=_WRAPPER)
    builder.add_input("workflow_input")
    builder.add_output("result")
    builder.add_link(
        "workflow_input", "envelope.envelope_input", link_type="direct", auto_transfer=True
    )
    builder.add_link("envelope.workflow_result", "result", link_type="direct", auto_transfer=True)
    builder.add_trigger(step_name="envelope", trigger_type="data_updated")
    return builder.load()


@pytest.mark.asyncio
async def test_provenance_and_events_are_captured_and_summarized():
    wf = _build_envelope_workflow("prov_demo")
    prov_run = await run_with_provenance(
        wf,
        {"workflow_input": {"markdown": "hello provenance"}},
        timeout=30.0,
        settle_ms=500,
        await_cascade=True,
    )

    assert prov_run.result.get("status") in {"completed", "completed_no_await"}

    # Auto-capture actually fired — NOT a silently-empty provenance context.
    assert len(prov_run.step_records) >= 1, (
        "no provenance records captured — G4 auto-capture is not wired into the run path; "
        "a 'what ran' view would be silently empty"
    )
    record_names = {r.get("step_name") for r in prov_run.step_records}
    assert "envelope" in record_names

    # G37 events captured for the step lifecycle.
    ev_types = {(e.step_name, e.event_type) for e in prov_run.step_events}
    assert ("envelope", "step_start") in ev_types
    assert ("envelope", "step_complete") in ev_types

    # EO-42 run summary folds records + events into the scientist-facing view.
    summary = summarize_run(prov_run)
    assert summary.workflow_status in {"completed", "completed_no_await"}
    env = [s for s in summary.steps if s.step_name == "envelope"]
    assert env, summary
    assert env[0].status == "completed"
