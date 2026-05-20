"""Integration test (EO-03 core): run_workflow_observed ties run + envelope + summary."""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.runtime.observed_run import run_workflow_observed

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

    builder = WorkflowBuilder(name=name, description="Observed-run demo.")
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
async def test_run_workflow_observed_returns_envelope_and_summary():
    wf = _build_envelope_workflow("observed_demo")
    outcome = await run_workflow_observed(
        wf,
        {"workflow_input": {"markdown": "observed hello"}},
        timeout=30.0,
        settle_ms=500,
        await_cascade=True,
    )

    # The LLM-facing envelope was extracted from the workflow output.
    assert outcome.workflow_result is not None
    assert outcome.workflow_result.markdown == "observed hello"
    assert outcome.workflow_result.status == "ok"

    # The visibility summary reflects the run.
    assert outcome.run_summary.workflow_status in {"completed", "completed_no_await"}
    env = [s for s in outcome.run_summary.steps if s.step_name == "envelope"]
    assert env, outcome.run_summary
    assert env[0].status == "completed"
