"""CGU-P2-T1 — unit tests for BenchmarkReviewerStep.

Pins:
  1. Loads via from_config with the bundled default reviewer prompt.
  2. Empty / missing code_spec raises ValueError.
  3. Empty / missing code_source raises ValueError.
  4. Trigger-envelope unwrap works (single-key wrapping).
  5. ``<think>...</think>`` blocks stripped from critique.
  6. Empty LLM response raises (no silent passthrough).
  7. Output schema: code_spec passthrough, previous_attempt = candidate,
     critique populated, optional fields passthrough.
  8. ``extra='forbid'`` rejects YAML typos.
  9. Unknown role rejected.
 10. review_revise workflow YAML loads via framework.

LLM mocked via monkeypatch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.benchmark_reviewer_step import (
    BenchmarkReviewerStep,
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
        "apecx_integration.composition.steps.benchmark_reviewer_step.build_chat_llm",
        _factory,
    )


def _stage(tmp_path: Path, *, yaml_extras: str = "") -> BenchmarkReviewerStep:
    body = "name: benchmark_reviewer_test\n" + yaml_extras
    p = tmp_path / "reviewer.yml"
    p.write_text(body)
    return BenchmarkReviewerStep.from_config(str(p))


def test_loads_with_default_prompt(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "benchmark_reviewer_test"
    assert "reviewer" in step.system_prompt.lower()


def test_empty_code_spec_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "irrelevant")
    with pytest.raises(ValueError, match="code_spec"):
        asyncio.run(step.process({"code_spec": "", "code_source": "def f(): pass"}))


def test_empty_code_source_raises(tmp_path, monkeypatch):
    """Cannot review nothing — surfaces as a clear error rather than
    silently producing a critique of empty code."""
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "irrelevant")
    with pytest.raises(ValueError, match="code_source"):
        asyncio.run(step.process({"code_spec": "Write f", "code_source": ""}))


def test_trigger_envelope_unwrap(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "- function name wrong\n- missing return\n")
    out = asyncio.run(
        step.process(
            {
                "reviewer_input": {
                    "code_spec": "Write f",
                    "code_source": "def g(): pass",
                }
            }
        )
    )
    assert "function name wrong" in out["critique"]


def test_strips_think_blocks(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    response = "<think>analyzing...</think>\n- issue A\n- issue B\n"
    _patch_llm(monkeypatch, response)
    out = asyncio.run(step.process({"code_spec": "Write f", "code_source": "def f(): pass"}))
    assert "analyzing" not in out["critique"]
    assert "issue A" in out["critique"]


def test_empty_llm_response_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "")
    with pytest.raises(ValueError, match="empty critique"):
        asyncio.run(step.process({"code_spec": "Write f", "code_source": "def f(): pass"}))


def test_output_schema_passthrough_and_critique(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "PASS")
    out = asyncio.run(
        step.process(
            {
                "code_spec": "Write f",
                "code_source": "def f(): return 1",
                "entry_point": "f",
                "test_hint": "assert f() == 1",
                "function_signature": "def f(): ...",
            }
        )
    )
    assert out["code_spec"] == "Write f"
    assert out["previous_attempt"] == "def f(): return 1"
    assert out["critique"] == "PASS"
    assert out["entry_point"] == "f"
    assert out["test_hint"] == "assert f() == 1"
    assert out["function_signature"] == "def f(): ..."


def test_yaml_typo_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)temperaturee|extra"):
        _stage(tmp_path, yaml_extras="temperaturee: 0.5\n")


def test_unknown_role_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)role"):
        _stage(tmp_path, yaml_extras="role: reviewerr\n")


def test_review_revise_workflow_yaml_loads():
    from nanobrain.core.workflow import Workflow  # noqa: PLC0415

    yaml_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "benchmark_review_revise"
        / "workflow.yml"
    )
    assert yaml_path.is_file(), yaml_path
    wf = Workflow.from_config(str(yaml_path))
    assert wf.name == "benchmark_review_revise"
    children = (
        getattr(wf, "child_steps", None)
        or getattr(wf, "_child_steps", None)
        or getattr(wf, "steps", None)
        or {}
    )
    assert "drafter" in children
    assert "reviewer" in children
    assert "reviser" in children
