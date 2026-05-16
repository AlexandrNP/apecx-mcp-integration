"""Integration tests for the Open-Rosalind → Rhea standalone case.

Three test surfaces:

1. **Workflow-load tests** (unconditional) — the YAML workflow and the
   lightweight-builder variant both load + validate. Loading does NOT
   need a Rhea worker (the RheaAdapter is resolved at process() time,
   not load time).

2. **Fake-adapter cascade test** (unconditional) — registers a fake
   ``BACKEND_NAME='rhea'`` adapter that returns a canned result, then
   drives the workflow end-to-end. This proves the workflow topology +
   ToolExecutionStep + adapter-dispatch path is correct WITHOUT a live
   Rhea worker.

3. **Live integration test** (gated on ``$RHEA_MCP_URL``) — runs the
   workflow against a real Rhea worker. Skipped with a loud reason
   when the env var is unset. Mirrors the gating pattern of
   nanobrain's test_rhea_mcp_dispatcher.py.

The rhea_workflow CODEGEN (the "code generator uses Rhea as an MCP
server") is factory-tested here too: it must FAIL LOUD at factory
time when $RHEA_MCP_URL is unset.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_YML = (
    REPO_ROOT / "src/apecx_integration/composition/workflows/open_rosalind_rhea/workflow.yml"
)

_RHEA_URL = os.environ.get("RHEA_MCP_URL")
_rhea_gate = pytest.mark.skipif(
    _RHEA_URL is None,
    reason="RHEA_MCP_URL not set — live Rhea worker required for this test",
)


# ---- 1. Workflow-load tests (unconditional) ----


def test_or_rhea_yaml_workflow_loads():
    if not WORKFLOW_YML.is_file():
        pytest.skip(f"workflow YAML missing: {WORKFLOW_YML}")
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(str(WORKFLOW_YML))
    assert wf.name == "open_rosalind_rhea"
    assert sorted(wf.child_steps.keys()) == ["sequence_tool"]
    assert len(wf.step_links) == 2
    step = wf.child_steps["sequence_tool"]
    # The UTD resolved + the backend is 'rhea'.
    assert step.utd.descriptor_id == "rhea:sequence.analyze@1.0.0"
    assert step.backend_name == "rhea"


def test_or_rhea_lightweight_builder_parity():
    from nanobrain.core.workflow import Workflow

    from apecx_integration.composition.workflows.open_rosalind_rhea_lightweight_builder import (
        build_open_rosalind_rhea_workflow_lightweight,
    )

    yaml_wf = Workflow.from_config(str(WORKFLOW_YML))
    lw_wf = build_open_rosalind_rhea_workflow_lightweight()
    # Topology parity: same step ids, same link count.
    assert sorted(yaml_wf.child_steps.keys()) == sorted(lw_wf.child_steps.keys())
    assert len(yaml_wf.step_links) == len(lw_wf.step_links) == 2


# ---- 2. Fake-adapter cascade test (unconditional) ----


def test_or_rhea_workflow_cascade_with_fake_adapter():
    """Drive the workflow end-to-end with a fake rhea backend adapter.

    Proves the topology: workflow_input -> ToolExecutionStep (resolves
    the rhea adapter, calls invoke) -> workflow_output. No live Rhea.
    """
    if not WORKFLOW_YML.is_file():
        pytest.skip(f"workflow YAML missing: {WORKFLOW_YML}")

    from nanobrain.core.workflow import Workflow
    from nanobrain.library.steps.tool_execution_step import (
        ToolBackendAdapter,
        ToolBackendRegistry,
    )

    class _FakeRheaAdapter(ToolBackendAdapter):
        BACKEND_NAME = "rhea"

        def __init__(self):
            self.calls: list[dict] = []

        async def invoke(self, utd, inputs, *, run_context_namespace="", **kwargs):
            self.calls.append(inputs)
            # Canned sequence.analyze-shaped result.
            return {
                "return": {
                    "type": "dna",
                    "length": len(inputs.get("sequence", "")),
                    "summary": "dna, length " + str(len(inputs.get("sequence", ""))),
                }
            }

    fake = _FakeRheaAdapter()
    # Snapshot + restore the registry around the test.
    had_rhea = "rhea" in ToolBackendRegistry.list_backends()
    if had_rhea:
        ToolBackendRegistry.unregister("rhea")
    ToolBackendRegistry.register(fake)
    try:

        async def _drive():
            wf = Workflow.from_config(str(WORKFLOW_YML))
            init = await wf.process({"sequence_tool_input": {"sequence": "ATGAAACGT"}})
            assert init.get("status") == "data_flow_initiated"
            drained = await wf.wait_for_cascade(timeout=15.0, settle_ms=150)
            assert drained, "OR-Rhea workflow cascade failed to drain"
            step = wf.child_steps["sequence_tool"]
            return await step.step_output_data_units["sequence_tool_output"].get()

        out = asyncio.run(_drive())
        # The fake adapter was invoked at least once, and EVERY call
        # received the CORRECTLY UNWRAPPED input (not the trigger
        # envelope {sequence_tool_input: {...}}). The cascade may fire
        # the step more than once — a known framework trigger quirk;
        # the adapter is idempotent so we assert per-call shape, not
        # call count.
        assert fake.calls, "fake rhea adapter was never invoked"
        for call in fake.calls:
            assert call == {"sequence": "ATGAAACGT"}, (
                f"ToolExecutionStep did not unwrap the trigger envelope: {call!r}"
            )
        # The workflow carried the adapter's result through.
        assert isinstance(out, dict)
        assert out["return"]["type"] == "dna"
        assert out["return"]["length"] == 9
    finally:
        ToolBackendRegistry.unregister("rhea")


# ---- 3. rhea_workflow codegen factory: fails loud without Rhea ----


def test_rhea_workflow_codegen_fails_loud_without_env(monkeypatch):
    monkeypatch.delenv("RHEA_MCP_URL", raising=False)
    from nanobrain.core.component_base import ComponentConfigurationError

    from tests.benchmarks.codegen.rhea_workflow import make_rhea_workflow_codegen

    with pytest.raises(ComponentConfigurationError, match="RHEA_MCP_URL"):
        make_rhea_workflow_codegen()


# ---- 4. Live integration test (gated on $RHEA_MCP_URL) ----


@_rhea_gate
def test_or_rhea_workflow_against_live_rhea():
    """End-to-end against a real Rhea worker. Requires the worker to be
    running AND hosting Open-Rosalind's sequence.analyze tool."""
    from nanobrain.core.workflow import Workflow
    from nanobrain.library.steps.tool_execution_step import ToolBackendRegistry
    from nanobrain.library.tools.rhea_adapter import RheaAdapter

    if "rhea" not in ToolBackendRegistry.list_backends():
        RheaAdapter.from_env(register=True)

    async def _drive():
        wf = Workflow.from_config(str(WORKFLOW_YML))
        await wf.process({"sequence_tool_input": {"sequence": "ATGAAACGT"}})
        drained = await wf.wait_for_cascade(timeout=120.0, settle_ms=100)
        assert drained, "live Rhea cascade did not drain in 120s"
        step = wf.child_steps["sequence_tool"]
        return await step.step_output_data_units["sequence_tool_output"].get()

    result = asyncio.run(_drive())
    assert result is not None, (
        "live Rhea worker returned nothing — check that it hosts the "
        "Open-Rosalind sequence.analyze tool"
    )
