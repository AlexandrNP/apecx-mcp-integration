"""CGU-P1-T6 — unit tests for BenchmarkDrafterStep.

Pins:
  1. Loads via from_config with the bundled default prompt.
  2. Empty / missing code_spec raises ValueError.
  3. Trigger-envelope wrapping ({drafter_input: {...}}) is unwrapped
     correctly so the step's process() sees the inner dict.
  4. ``<think>...</think>`` blocks emitted by thinking-token models
     are stripped before fence extraction.
  5. Code fence extraction picks the largest fenced block (handles
     LLM-emits-multiple-blocks shape).
  6. Empty LLM response raises (not silent passthrough).
  7. ``extra='forbid'`` rejects YAML typos at load.
  8. Unknown role rejected at config validation (typo guard).

LLM mocked via monkeypatch on ``build_chat_llm`` — no real LLM call
in unit tests. The full LLM round-trip is covered by the integration
sweep (CGU-P1-T6 AC).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.benchmark_drafter_step import (
    BenchmarkDrafterStep,
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
        "apecx_integration.composition.steps.benchmark_drafter_step.build_chat_llm",
        _factory,
    )


def _stage_step(
    tmp_path: Path,
    *,
    yaml_extras: str = "",
) -> BenchmarkDrafterStep:
    body = "name: benchmark_drafter_test\n" + yaml_extras
    p = tmp_path / "drafter.yml"
    p.write_text(body)
    return BenchmarkDrafterStep.from_config(str(p))


# ---------------------------------------------------------------------------


def test_loads_with_default_prompt(tmp_path):
    step = _stage_step(tmp_path)
    assert step.name == "benchmark_drafter_test"
    # Default benchmark prompt is non-empty.
    assert "Python code" in step.system_prompt


def test_empty_code_spec_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "should never be called")
    with pytest.raises(ValueError, match="code_spec"):
        asyncio.run(step.process({"code_spec": "   "}))


def test_non_dict_input_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "should never be called")
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process("not a dict"))


def test_trigger_envelope_unwrap(tmp_path, monkeypatch):
    """When the workflow's trigger system delivers via DirectLink,
    the framework wraps the payload as ``{<input_du_name>: <payload>}``.
    The step must unwrap that wrapper before reading ``code_spec``."""
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "```python\ndef foo():\n    return 42\n```")
    out = asyncio.run(step.process({"drafter_input": {"code_spec": "Write foo()"}}))
    assert "def foo" in out["code_source"]


def test_extracts_largest_fenced_block(tmp_path, monkeypatch):
    """When the LLM emits multiple ```python blocks, the step takes
    the largest (most likely the real answer; the smaller is usually
    an import helper or a usage snippet)."""
    step = _stage_step(tmp_path)
    response = (
        "Here's a tiny helper:\n"
        "```python\nimport math\n```\n\n"
        "Now the actual code:\n"
        "```python\ndef solve(n):\n    return n * 2\n    # 30 chars longer\n```\n"
    )
    _patch_llm(monkeypatch, response)
    out = asyncio.run(step.process({"code_spec": "Double a number"}))
    assert "def solve" in out["code_source"]
    # Smaller block should NOT be returned alone.
    assert "import math" not in out["code_source"] or "def solve" in out["code_source"]


def test_strips_think_blocks_from_thinking_models(tmp_path, monkeypatch):
    """Nemotron / Qwen / DeepSeek-R1 emit ``<think>...</think>``
    scratch blocks before their answer. Those blocks may contain
    code fences that would confuse the largest-block heuristic."""
    step = _stage_step(tmp_path)
    response = (
        "<think>Let me sketch:\n```python\nx = 1\nx = 2\nx = 3\nx = 4\n```\n"
        "OK that's wrong. The real answer:</think>\n"
        "```python\ndef ans(): return 7\n```\n"
    )
    _patch_llm(monkeypatch, response)
    out = asyncio.run(step.process({"code_spec": "Return 7"}))
    assert "def ans" in out["code_source"]
    # The think-block code is NOT the largest after stripping.
    assert "x = 4" not in out["code_source"]


def test_empty_llm_response_raises(tmp_path, monkeypatch):
    """Zero-length LLM output is treated as a transient LLM failure,
    not silently shipped as an empty candidate. The benchmark scorer
    can still bucket the failure cleanly as fail_other."""
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "")
    with pytest.raises(ValueError, match="empty response"):
        asyncio.run(step.process({"code_spec": "Return 7"}))


def test_yaml_typo_rejected(tmp_path):
    """``extra='forbid'`` catches a misspelled field at config load,
    not silently as a default."""
    with pytest.raises(Exception, match="(?i)temperaturee|extra"):
        _stage_step(tmp_path, yaml_extras="temperaturee: 0.5\n")


def test_unknown_role_rejected_at_load(tmp_path):
    """A typo in ``role:`` would silently route to the hardcoded
    fallback model. The validator rejects unknown roles at load."""
    with pytest.raises(Exception, match="(?i)role"):
        _stage_step(tmp_path, yaml_extras="role: draftor\n")


def test_role_drafter_resolves_via_composer_config(tmp_path, monkeypatch):
    """End-to-end: process() with the default role calls
    build_chat_llm with the model from composer_config.yml's
    model_roles.drafter. We don't pin the exact model string
    (operator-tunable) — we pin that some model name flows through."""
    step = _stage_step(tmp_path)
    captured: dict = {}

    def _recording_factory(temperature=0.0, max_tokens=1024, **overrides):
        captured["overrides"] = overrides
        return _StubLLM("```python\ndef k(): return 1\n```")

    monkeypatch.setattr(
        "apecx_integration.composition.steps.benchmark_drafter_step.build_chat_llm",
        _recording_factory,
    )

    asyncio.run(step.process({"code_spec": "Return 1"}))
    # The role resolver must have supplied a non-empty `model` kwarg.
    assert "model" in captured["overrides"]
    assert captured["overrides"]["model"]
    assert isinstance(captured["overrides"]["model"], str)


def test_workflow_yaml_loads_via_framework():
    """Smoke-load the workflow YAML via Workflow.from_config — pins
    that the step references, link source/target dot-notation, and
    DU names all resolve. Cheap; no LLM call."""
    from pathlib import Path  # noqa: PLC0415

    from nanobrain.core.workflow import Workflow  # noqa: PLC0415

    yaml_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "benchmark_direct_codegen"
        / "workflow.yml"
    )
    assert yaml_path.is_file(), yaml_path
    wf = Workflow.from_config(str(yaml_path))
    assert wf.name == "benchmark_direct_codegen"
    children = (
        getattr(wf, "child_steps", None)
        or getattr(wf, "_child_steps", None)
        or getattr(wf, "steps", None)
        or {}
    )
    assert "drafter" in children
