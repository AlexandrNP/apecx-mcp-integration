"""Deterministic no-LLM reproducer for the nested SubworkflowStep hang.

Goal: isolate the outer-workflow cascade failure WITHOUT any LLM.
If this hangs, the bug is in framework wiring. If it passes, the
LLM-bound integration test's failure is specific to LLM-step
timing or apecx-specific workflow YAMLs.

Two-level composition:
  outer_workflow
    step_a (SubworkflowStep) → inner_workflow_a (EchoStep)
    step_b (SubworkflowStep) → inner_workflow_b (EchoStep)

EchoStep returns its input plus a trace marker in <1ms. Total
expected wall time: <5s. Anything longer is a deadlock.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep
from nanobrain.library.steps.subworkflow_step import SubworkflowStep


class EchoStep(BaseStep):
    """Test-only step. Echoes input with a trace marker.

    Deep-unwraps single-key wrapper dicts repeatedly until it finds a
    dict with the data keys it needs. This is the test-fixture
    equivalent of what production steps (CodeWriteStep, etc.) do
    with their own unwrap logic — it walks through any chain of
    framework + SubworkflowStep wrapping envelopes.
    """

    COMPONENT_TYPE: str = "echo_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        # Walk through single-key wrapper dicts until we find a dict
        # that actually contains "value" OR "trace" OR we've gone 5
        # levels deep (paranoia).
        for _ in range(5):
            if (
                isinstance(input_data, dict)
                and len(input_data) == 1
                and "value" not in input_data
                and "trace" not in input_data
            ):
                sole_value = next(iter(input_data.values()))
                if isinstance(sole_value, dict):
                    input_data = sole_value
                    continue
            break
        return {
            "value": input_data.get("value", "<no-value>"),
            "step_marker": self.name,
            "trace": (input_data.get("trace") or []) + [self.name],
        }


class _SubA(SubworkflowStep):
    COMPONENT_TYPE: str = "_sub_a"


class _SubB(SubworkflowStep):
    COMPONENT_TYPE: str = "_sub_b"


def _stage_echo_step_yml(tmp_path: Path, name: str) -> Path:
    """Stage a step YAML for EchoStep with given name."""
    p = tmp_path / f"echo_{name}.yml"
    p.write_text(
        f"""\
class: "tests.integration.test_nested_subworkflow_reproducer.EchoStep"
name: echo_{name}
input_data_units:
  echo_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: echo_input
    persistent: false
output_data_units:
  echo_output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: echo_output
    persistent: false
triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "echo_input"
"""
    )
    return p


def _stage_inner_workflow(tmp_path: Path, name: str) -> Path:
    echo_yml = _stage_echo_step_yml(tmp_path, name)
    p = tmp_path / f"inner_{name}.yml"
    p.write_text(
        f"""\
name: inner_{name}
description: "Deterministic no-LLM inner workflow."
version: "0.1.0"
config_version: 2

input_data_units:
  workflow_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: workflow_input
    persistent: false

output_data_units:
  workflow_output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: workflow_output
    persistent: false

steps:
  echo:
    class: "tests.integration.test_nested_subworkflow_reproducer.EchoStep"
    config: "{echo_yml}"

links:
  in_to_echo:
    class: "nanobrain.core.link.DirectLink"
    config:
      link_type: direct
      source: workflow_input
      target: echo.echo_input
      auto_transfer: true
  echo_to_out:
    class: "nanobrain.core.link.DirectLink"
    config:
      link_type: direct
      source: echo.echo_output
      target: workflow_output
      auto_transfer: true
"""
    )
    return p


def _stage_sub_step_yml(tmp_path: Path, *, role: str, class_path: str, inner_path: Path) -> Path:
    p = tmp_path / f"{role}.yml"
    p.write_text(
        f"""\
class: "{class_path}"
name: {role}
inner_workflow_path: "{inner_path}"
timeout_seconds: 10.0
input_data_units:
  {role}_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: {role}_input
    persistent: false
output_data_units:
  {role}_output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: {role}_output
    persistent: false
triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "{role}_input"
"""
    )
    return p


def test_nested_subworkflow_chain_completes_without_llm(tmp_path):
    """Direct reproducer of the outer-cascade hang. No LLM in the loop.

    Pass criterion: outer cascade completes in <15s with both
    sub_a and sub_b having executed (trace contains both markers).
    """
    from nanobrain.core.workflow import Workflow

    inner_a = _stage_inner_workflow(tmp_path, "a")
    inner_b = _stage_inner_workflow(tmp_path, "b")
    sub_a_yml = _stage_sub_step_yml(
        tmp_path,
        role="sub_a",
        class_path="tests.integration.test_nested_subworkflow_reproducer._SubA",
        inner_path=inner_a,
    )
    sub_b_yml = _stage_sub_step_yml(
        tmp_path,
        role="sub_b",
        class_path="tests.integration.test_nested_subworkflow_reproducer._SubB",
        inner_path=inner_b,
    )

    outer_yml = tmp_path / "outer.yml"
    outer_yml.write_text(
        f"""\
name: outer_chain
description: "Two SubworkflowStep instances chained at workflow level."
version: "0.1.0"
config_version: 2

input_data_units:
  workflow_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: workflow_input
    persistent: false

output_data_units:
  final_output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: final_output
    persistent: false

steps:
  sub_a:
    class: "tests.integration.test_nested_subworkflow_reproducer._SubA"
    config: "{sub_a_yml}"
  sub_b:
    class: "tests.integration.test_nested_subworkflow_reproducer._SubB"
    config: "{sub_b_yml}"

links:
  in_to_a:
    class: "nanobrain.core.link.DirectLink"
    config:
      link_type: direct
      source: workflow_input
      target: sub_a.sub_a_input
      auto_transfer: true
  a_to_b:
    class: "nanobrain.core.link.DirectLink"
    config:
      link_type: direct
      source: sub_a.sub_a_output
      target: sub_b.sub_b_input
      auto_transfer: true
  b_to_out:
    class: "nanobrain.core.link.DirectLink"
    config:
      link_type: direct
      source: sub_b.sub_b_output
      target: final_output
      auto_transfer: true
"""
    )

    async def _drive():
        wf = Workflow.from_config(str(outer_yml))
        await wf.process({"sub_a_input": {"value": "hello", "trace": []}})
        sub_b = wf.child_steps["sub_b"]
        out_du = sub_b.step_output_data_units["sub_b_output"]
        deadline = asyncio.get_event_loop().time() + 15.0
        while True:
            v = await out_du.get()
            if v is not None:
                return v
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError("outer cascade did not propagate sub_b output in 15s")
            await asyncio.sleep(0.1)

    start = time.monotonic()
    result = asyncio.run(_drive())
    elapsed = time.monotonic() - start

    assert elapsed < 15.0
    assert isinstance(result, dict)
    # The result is wrapped one level deep because the inner workflow's
    # workflow-level output_data_unit is named "workflow_output" and
    # SubworkflowStep collects it under that key. Unwrap to find the
    # final data.
    payload = result.get("workflow_output", result)
    trace = payload.get("trace") or []
    print(f"\n[nested-reproducer] elapsed={elapsed:.2f}s; trace={trace}; payload={payload}")
    assert "echo_a" in trace, (
        f"sub_a did not execute or its trace was lost; trace={trace}; full result={result}"
    )
    assert "echo_b" in trace, f"sub_b did not execute; trace={trace}; full result={result}"
    assert payload.get("value") == "hello", (
        f"value did not propagate through both cascades; payload={payload}"
    )
