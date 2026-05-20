"""Integration test (EO-13b): EnvelopeStep inside a real lightweight-built workflow,
driven by Workflow.run(), demonstrating A->B handle chaining.

This is framework-native end-to-end coverage (not a mock): a workflow built via nanobrain's
``WorkflowBuilder`` runs through the real trigger/link cascade. It proves:

1. A real workflow emits a WorkflowResult through the cascade.
2. A structured payload is stashed behind a handle and is ABSENT from the markdown channel
   the orchestrating LLM sees.
3. The handle round-trips the FULL payload — so a downstream consumer (workflow B, played
   here by the orchestrator) gets the complete data without it transiting A's LLM-visible
   output. That is the two-channel chaining contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.handles.store import default_handle_store
from apecx_integration.composition.schemas.data_shapes import RecordSet
from apecx_integration.composition.schemas.workflow_result import WorkflowResult

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
    """One-step workflow: workflow_input -> envelope -> result. Lightweight build."""
    from nanobrain.lightweight import WorkflowBuilder

    builder = WorkflowBuilder(name=name, description="Envelope demo (programmatic build).")
    builder.add_step("envelope", ENVELOPE_STEP_CLASS, description="wrap output", config=_WRAPPER)
    builder.add_input("workflow_input")
    builder.add_output("result")
    builder.add_link(
        "workflow_input", "envelope.envelope_input", link_type="direct", auto_transfer=True
    )
    builder.add_link("envelope.workflow_result", "result", link_type="direct", auto_transfer=True)
    builder.add_trigger(step_name="envelope", trigger_type="data_updated")
    return builder.load()


@pytest.fixture(autouse=True)
def _clear_store():
    default_handle_store().clear()
    yield
    default_handle_store().clear()


@pytest.mark.asyncio
async def test_envelope_workflow_runs_and_chains_via_handle():
    secret = "SENSITIVE_SEQUENCE_ABCDEF"

    # --- Workflow A: produce a 50-record payload, stash behind a handle. ---
    wf_a = _build_envelope_workflow("envelope_demo_a")
    rs = RecordSet(records=[{"seq": secret} for _ in range(50)], columns=["seq"])
    run_a = await wf_a.run(
        {"workflow_input": {"markdown": "found 50 records", "data": rs.model_dump(mode="json")}},
        timeout=30.0,
        settle_ms=500,
        await_cascade=True,
    )

    assert run_a.get("status") in {"completed", "completed_no_await"}, run_a
    result_a = WorkflowResult.model_validate(run_a["result"])

    # (2) channel separation through the REAL cascade: full payload not in markdown.
    assert secret not in result_a.markdown
    assert result_a.data_handle is not None
    assert result_a.data_preview is not None and result_a.data_preview["count"] == 50

    # --- Orchestrator chains A -> B: passes ONLY the handle forward. ---
    # The orchestrating LLM saw markdown + preview, not the 50 rows. The downstream
    # retrieves the full payload from the store via the handle.
    retrieved = default_handle_store().get(result_a.data_handle)
    assert isinstance(retrieved, RecordSet)
    assert len(retrieved.records) == 50
    assert retrieved.records[0]["seq"] == secret

    # --- Workflow B: summarize the retrieved payload (data informed B without ---
    # --- ever transiting A's LLM-visible output). ---
    wf_b = _build_envelope_workflow("envelope_demo_b")
    run_b = await wf_b.run(
        {
            "workflow_input": {
                "markdown": f"processed {len(retrieved.records)} records from upstream"
            }
        },
        timeout=30.0,
        settle_ms=500,
        await_cascade=True,
    )
    assert run_b.get("status") in {"completed", "completed_no_await"}, run_b
    result_b = WorkflowResult.model_validate(run_b["result"])
    assert "processed 50 records" in result_b.markdown
