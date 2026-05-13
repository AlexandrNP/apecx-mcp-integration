"""Unit tests for TaskCategoryRouterStep -- deterministic classifier."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.task_category_router_step import (
    TaskCategoryRouterStep,
)


def _stage(tmp_path: Path) -> TaskCategoryRouterStep:
    p = tmp_path / "v.yml"
    p.write_text("name: router_test\n")
    return TaskCategoryRouterStep.from_config(str(p))


def test_loads(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "router_test"


def test_empty_code_spec_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="empty code_spec"):
        asyncio.run(step.process({"code_spec": "", "entry_point": "X"}))


def test_classifies_step(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process({"code_spec": "Write a BaseStep called UpperStep", "entry_point": "UpperStep"})
    )
    assert out["task_category"] == "step"
    assert "BaseStep" in out["code_spec"]


def test_classifies_tool(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process({"code_spec": "Write a ToolBase subclass", "entry_point": "CalculatorTool"})
    )
    assert out["task_category"] == "tool"


def test_classifies_config_precedes_step(tmp_path):
    """ThresholdStepConfig should route to config even though it
    contains 'Step' as a substring."""
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process(
            {
                "code_spec": "Subclass StepConfig to add a threshold field",
                "entry_point": "ThresholdStep",
            }
        )
    )
    assert out["task_category"] == "config"


def test_classifies_builder(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process(
            {"code_spec": "Build a workflow with WorkflowBuilder", "entry_point": "build_workflow"}
        )
    )
    assert out["task_category"] == "builder"


def test_classifies_default(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process({"code_spec": "Write a sorting function", "entry_point": "sort_list"})
    )
    assert out["task_category"] == "default"


def test_enriched_code_spec_contains_example(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"code_spec": "Write UpperStep", "entry_point": "UpperStep"}))
    # The enriched spec includes the worked example.
    assert "Reference" in out["code_spec"]
    assert "class UpperStep(BaseStep):" in out["code_spec"]


def test_missing_examples_dir_fails_at_load(tmp_path):
    """A bad examples_dir should fail at config-load, not at process."""
    p = tmp_path / "v.yml"
    p.write_text(f"name: bad\nexamples_dir: '{tmp_path / 'nonexistent'}'\n")
    with pytest.raises(ValueError, match="example file missing"):
        TaskCategoryRouterStep.from_config(str(p))
