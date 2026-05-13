"""CGU-P1-T6 — unit tests for BenchmarkPlannerStep.

Pins:
  1. Loads via from_config with the bundled default planner prompt.
  2. Empty / missing code_spec raises ValueError.
  3. Trigger-envelope wrap unwrap.
  4. ``<think>...</think>`` blocks stripped from planner output.
  5. Code fences in planner output stripped (planner should emit
     plain text; fences are noise).
  6. Empty LLM response raises ValueError (no silent passthrough).
  7. Output enriches code_spec with plan; preserves passthrough fields.
  8. ``extra='forbid'`` rejects YAML typos.
  9. Unknown role rejected at config validation.
 10. plan-then-code workflow YAML loads via Workflow.from_config.

LLM mocked via monkeypatch — no real LLM call in unit tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.benchmark_planner_step import (
    BenchmarkPlannerStep,
)


class _StubLLM:
    def __init__(self, response_text: str):
        self._response_text = response_text

    def invoke(self, _messages):
        class _R:
            content = self._response_text

        return _R()


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    def _factory(temperature=0.0, max_tokens=1024, **overrides):
        return _StubLLM(response_text)

    monkeypatch.setattr(
        "apecx_integration.composition.steps.benchmark_planner_step.build_chat_llm",
        _factory,
    )


def _stage(tmp_path: Path, *, yaml_extras: str = "") -> BenchmarkPlannerStep:
    body = "name: benchmark_planner_test\n" + yaml_extras
    p = tmp_path / "planner.yml"
    p.write_text(body)
    return BenchmarkPlannerStep.from_config(str(p))


def test_loads_with_default_prompt(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "benchmark_planner_test"
    assert "numbered plan" in step.system_prompt.lower()


def test_empty_code_spec_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "irrelevant")
    with pytest.raises(ValueError, match="code_spec"):
        asyncio.run(step.process({"code_spec": "  "}))


def test_trigger_envelope_unwrap(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "1. Parse input.\n2. Sort.\n3. Return.")
    out = asyncio.run(step.process({"planner_input": {"code_spec": "Sort a list of ints"}}))
    assert "plan" in out
    assert "Sort" in out["plan"]


def test_strips_think_blocks(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    response = (
        "<think>Let me think about this...</think>\n1. Read input.\n2. Compute.\n3. Return.\n"
    )
    _patch_llm(monkeypatch, response)
    out = asyncio.run(step.process({"code_spec": "Compute X"}))
    assert "Let me think" not in out["plan"]
    assert "1. Read input." in out["plan"]


def test_strips_code_fences(tmp_path, monkeypatch):
    """If the planner accidentally emits a code fence, we strip it.
    The planner's job is plain text; fences are LLM drift."""
    step = _stage(tmp_path)
    response = "1. Parse.\n2. Process.\n3. Return.\n\n```python\ndef sample():\n    pass\n```\n"
    _patch_llm(monkeypatch, response)
    out = asyncio.run(step.process({"code_spec": "Sample task"}))
    assert "def sample" not in out["plan"]
    assert "1. Parse." in out["plan"]


def test_empty_llm_response_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "")
    with pytest.raises(ValueError, match="empty plan"):
        asyncio.run(step.process({"code_spec": "X"}))


def test_output_enriches_code_spec_and_preserves_passthrough(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "1. Step one.\n2. Step two.\n")
    out = asyncio.run(
        step.process(
            {
                "code_spec": "Solve X",
                "entry_point": "solve",
                "test_hint": "assert solve(1) == 1",
                "function_signature": "def solve(x): ...",
            }
        )
    )
    assert "Solve X" in out["code_spec"]
    assert "Suggested plan:" in out["code_spec"]
    assert "Step one" in out["code_spec"]
    assert out["entry_point"] == "solve"
    assert out["test_hint"] == "assert solve(1) == 1"
    assert out["function_signature"] == "def solve(x): ..."


def test_yaml_typo_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)temperaturee|extra"):
        _stage(tmp_path, yaml_extras="temperaturee: 0.5\n")


def test_unknown_role_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)role"):
        _stage(tmp_path, yaml_extras="role: planar\n")


def test_plan_then_code_workflow_yaml_loads():
    from nanobrain.core.workflow import Workflow  # noqa: PLC0415

    yaml_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "benchmark_plan_then_code"
        / "workflow.yml"
    )
    assert yaml_path.is_file(), yaml_path
    wf = Workflow.from_config(str(yaml_path))
    assert wf.name == "benchmark_plan_then_code"
    children = (
        getattr(wf, "child_steps", None)
        or getattr(wf, "_child_steps", None)
        or getattr(wf, "steps", None)
        or {}
    )
    assert "planner" in children
    assert "drafter" in children
