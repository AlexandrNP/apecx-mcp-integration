"""Integration test (EO-20 real impls): LocalDecomposer + KeywordWorkflowMatcher +
RunWorkflowDispatcher driving a real workflow via Workflow.run.

Makes two of the three decomposition boundaries real (matcher + dispatcher) without an LLM;
only the TaskDecomposer remains LLM-gated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.decomposition.dispatchers import RunWorkflowDispatcher
from apecx_integration.composition.decomposition.local_decomposer import LocalDecomposer, Task
from apecx_integration.composition.decomposition.matchers import KeywordWorkflowMatcher

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

    builder = WorkflowBuilder(name=name, description="Dispatch demo.")
    builder.add_step("envelope", ENVELOPE_STEP_CLASS, description="wrap output", config=_WRAPPER)
    builder.add_input("workflow_input")
    builder.add_output("result")
    builder.add_link(
        "workflow_input", "envelope.envelope_input", link_type="direct", auto_transfer=True
    )
    builder.add_link("envelope.workflow_result", "result", link_type="direct", auto_transfer=True)
    builder.add_trigger(step_name="envelope", trigger_type="data_updated")
    return builder.load()


class _NoDecompose:
    async def decompose(self, task: Task) -> list[Task]:
        return []


@pytest.mark.asyncio
async def test_dispatcher_runs_real_workflow():
    dispatcher = RunWorkflowDispatcher(
        workflow_loader=_build_envelope_workflow,
        timeout=30.0,
        settle_ms=500,
        await_cascade=True,
    )
    r = await dispatcher.dispatch(
        "env_wf", Task("x", payload={"workflow_input": {"markdown": "dispatched!"}})
    )
    assert r.status == "ok"
    assert r.markdown == "dispatched!"


@pytest.mark.asyncio
async def test_decomposer_matches_then_dispatches_for_real():
    matcher = KeywordWorkflowMatcher({"env_wf": "wrap markdown into an envelope result"})
    dispatcher = RunWorkflowDispatcher(
        _build_envelope_workflow, timeout=30.0, settle_ms=500, await_cascade=True
    )
    dec = LocalDecomposer(matcher, _NoDecompose(), dispatcher, match_threshold=0.05)
    r = await dec.solve(
        Task("wrap markdown envelope", payload={"workflow_input": {"markdown": "via decomposer"}})
    )
    assert r.status == "ok"
    assert r.markdown == "via decomposer"


@pytest.mark.asyncio
async def test_unknown_workflow_raises_loudly():
    def _loader(name: str):
        raise KeyError(f"unknown workflow: {name}")

    dispatcher = RunWorkflowDispatcher(_loader)
    with pytest.raises(KeyError):
        await dispatcher.dispatch("nope", Task("x", payload={}))
