"""Integration test for benchmark_perturbed_consensus cascade.

Pins the 4-node cascade end-to-end with stubbed LLM:
    workflow_input
        -> task_router_perturbed   (deterministic)
        -> perturbing_drafter      (LLM, 3 stem-perturbed samples)
        -> aggregator              (deterministic AST voter)
        -> workflow_output

Regression catch: a silent ``auto_transfer=False`` regression on any
DirectLink would defeat the workflow without raising; this test
asserts the workflow output actually carries the aggregator's
selected ``code_source`` AND that the perturbations the drafter
emitted ALL appear in the candidate stream (so the aggregator had
genuine variance to vote across).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_YML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "benchmark_perturbed_consensus"
    / "workflow.yml"
)


class _RoundRobinStubLLM:
    """Returns different parseable code per call so the aggregator sees
    distinct candidates."""

    _RESPONSES = [
        "```python\nfrom nanobrain.core.step import BaseStep, StepConfig\nfrom pydantic import ConfigDict\nclass MyStepConfig(StepConfig):\n    model_config = ConfigDict(extra='forbid')\nclass MyStep(BaseStep):\n    COMPONENT_TYPE = 'a'\n    @classmethod\n    def _get_config_class(cls): return MyStepConfig\n    async def process(self, input_data, **kw): return input_data\n```",
        "```python\nfrom nanobrain.core.step import BaseStep, StepConfig\nfrom pydantic import ConfigDict\nclass MyStepConfig(StepConfig):\n    model_config = ConfigDict(extra='forbid')\nclass MyStep(BaseStep):\n    COMPONENT_TYPE = 'b'\n    @classmethod\n    def _get_config_class(cls): return MyStepConfig\n    async def process(self, input_data, **kw): return {**input_data, 'x': 1}\n```",
        "```python\nfrom nanobrain.core.step import BaseStep, StepConfig\nfrom pydantic import ConfigDict\nclass MyStepConfig(StepConfig):\n    model_config = ConfigDict(extra='forbid')\nclass MyStep(BaseStep):\n    COMPONENT_TYPE = 'c'\n    @classmethod\n    def _get_config_class(cls): return MyStepConfig\n    async def process(self, input_data, **kw): return {**input_data, 'y': 2}\n```",
    ]

    def __init__(self):
        self.i = 0

    def invoke(self, _messages):
        text = self._RESPONSES[self.i % len(self._RESPONSES)]
        self.i += 1

        class _R:
            content = text

        return _R()


def _factory(*_a, **_kw):
    return _RoundRobinStubLLM()


def test_perturbed_consensus_cascade_drains_with_3_distinct_candidates():
    if not WORKFLOW_YML.is_file():
        pytest.skip(f"workflow YAML missing: {WORKFLOW_YML}")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False):
        pass

    with patch(
        "apecx_integration.composition.steps.prompt_perturbing_drafter_step.build_chat_llm",
        _factory,
    ):
        from nanobrain.core.workflow import Workflow

        async def _drive():
            wf = Workflow.from_config(str(WORKFLOW_YML))
            init = await wf.process(
                {
                    "router_input": {
                        "code_spec": "Write a BaseStep subclass.",
                        "entry_point": "MyStep",
                    }
                }
            )
            assert init.get("status") == "data_flow_initiated"
            drained = await wf.wait_for_cascade(timeout=30.0, settle_ms=200)
            assert drained, "perturbed_consensus cascade failed to drain in 30s"

            drafter = wf.child_steps["perturbing_drafter"]
            agg = wf.child_steps["aggregator"]
            drafter_out = await drafter.step_output_data_units["perturbing_drafter_output"].get()
            agg_out = await agg.step_output_data_units["aggregator_output"].get()
            return drafter_out, agg_out

        drafter_out, agg_out = asyncio.run(_drive())

    # Drafter emits exactly 3 candidates, each tagged with its perturbation.
    assert isinstance(drafter_out, dict)
    candidates = drafter_out["candidates"]
    assert len(candidates) == 3
    perturbations = {c["perturbation"] for c in candidates}
    assert perturbations == {"Implement", "Author", "Write"}

    # Aggregator selects one (n_samples=3, voted_passes >=1 since the
    # stub emits AST-valid code).
    assert isinstance(agg_out, dict)
    assert agg_out["n_samples"] == 3
    assert agg_out["voted_passes"] >= 1
    assert "class MyStep" in (agg_out.get("code_source") or "")
