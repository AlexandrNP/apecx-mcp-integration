"""Integration test: pin the 6-node integrated workflow's cascade.

Validates the F22 / post-F17 integrated workflow end-to-end with a
stubbed LLM so the test does NOT require Ollama:

    workflow_input
        -> task_router_integrated (deterministic)
        -> memory_reader          (deterministic; read mode)
        -> multi_drafter          (LLM, fan-out N=3, T=0.5)
        -> aggregator             (deterministic; AST voter)
        -> memory_recorder        (deterministic; record mode, gated)
        -> workflow_output / workflow_recorder_status

Why this test exists
--------------------

The integrated workflow is the highest-fan-out scaffold we ship.
The trigger cascade has to fire 6 times (one per node) and every
DirectLink has to carry ``auto_transfer: true``. A silent
``auto_transfer=False`` regression would defeat the workflow without
raising an exception (F17/F11 silent-failure shape).

This test:
* Loads the workflow YAML by ``from_config``.
* Drives ``router_input`` with a synthesized prompt.
* Stubs the LLM so the test is hermetic.
* Awaits ``wait_for_cascade`` and asserts both workflow outputs
  populated AND the memory store on disk has a new entry.

A regression that drops a DirectLink's ``auto_transfer`` will fail
this test immediately.
"""

from __future__ import annotations

import asyncio
import json
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
    / "benchmark_integrated_full"
    / "workflow.yml"
)


class _StubLLM:
    """Returns a deterministic, parseable BaseStep subclass on every call."""

    @staticmethod
    def invoke(_messages):
        class _R:
            content = (
                "```python\n"
                "from nanobrain.core.step import BaseStep, StepConfig\n"
                "from pydantic import ConfigDict\n"
                "\n"
                "class MyStepConfig(StepConfig):\n"
                "    model_config = ConfigDict(extra='forbid')\n"
                "\n"
                "class MyStep(BaseStep):\n"
                "    COMPONENT_TYPE = 'my_step'\n"
                "    @classmethod\n"
                "    def _get_config_class(cls):\n"
                "        return MyStepConfig\n"
                "    async def process(self, input_data, **kw):\n"
                "        return input_data\n"
                "```"
            )

        return _R()


def _factory(*_args, **_kwargs):
    return _StubLLM()


def test_integrated_workflow_cascade_drains_and_records():
    if not WORKFLOW_YML.is_file():
        pytest.skip(f"workflow YAML missing: {WORKFLOW_YML}")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as _tf:
        tmp_store = Path(_tf.name)
    rec_yml = WORKFLOW_YML.parent / "steps" / "memory_recorder.yml"
    read_yml = WORKFLOW_YML.parent / "steps" / "memory_reader.yml"
    orig_rec = rec_yml.read_text()
    orig_read = read_yml.read_text()
    try:
        # Point both memory sides at the temp store so the test is hermetic.
        for p in (rec_yml, read_yml):
            txt = p.read_text()
            p.write_text(txt + f'store_path: "{tmp_store}"\n')

        with patch(
            "apecx_integration.composition.steps.multi_sample_drafter_step.build_chat_llm",
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
                assert isinstance(init, dict)
                assert init.get("status") == "data_flow_initiated"
                drained = await wf.wait_for_cascade(timeout=30.0, settle_ms=200)
                assert drained, "trigger cascade failed to drain in 30s"

                agg = wf.child_steps["aggregator"]
                rec = wf.child_steps["memory_recorder"]
                wo = await agg.step_output_data_units["aggregator_output"].get()
                ws = await rec.step_output_data_units["memory_recorder_output"].get()
                return wo, ws

            wo, ws = asyncio.run(_drive())

        # Aggregator's output carries the winner + the routing + voting telemetry.
        assert isinstance(wo, dict), f"aggregator output not a dict: {type(wo)!r}"
        assert "class MyStep" in (wo.get("code_source") or ""), (
            "the stubbed LLM's code did not survive the cascade"
        )
        assert wo.get("task_category") == "step", (
            "task_category did not pass through the 6-node cascade — a "
            "silent regression in drafter/aggregator passthrough"
        )
        assert wo.get("n_samples") == 3
        assert wo.get("voted_passes") >= 1, "AST voter rejected all candidates"

        # Recorder side-effect surfaced via workflow_recorder_status port.
        assert isinstance(ws, dict)
        assert ws.get("recorded") is True
        assert ws.get("category") == "step"

        # On-disk memory store has the recorded solution.
        assert tmp_store.is_file() and tmp_store.stat().st_size > 0
        store = json.loads(tmp_store.read_text())
        assert "step" in store
        assert len(store["step"]) >= 1
        assert "class MyStep" in store["step"][-1]
    finally:
        rec_yml.write_text(orig_rec)
        read_yml.write_text(orig_read)
        if tmp_store.exists():
            tmp_store.unlink()
