"""Unit tests for MultiSampleDrafterStep (fan-out drafter)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.multi_sample_drafter_step import (
    MultiSampleDrafterStep,
)


class _StubLLM:
    """Stub LLM that returns a sequence of responses, one per call."""

    def __init__(self, responses: list[str]):
        self._responses = responses
        self._call_count = 0

    def invoke(self, _messages):
        idx = self._call_count
        self._call_count += 1
        text = self._responses[idx % len(self._responses)]

        class _R:
            content = text

        return _R()


def _patch_llm(monkeypatch, responses: list[str]) -> _StubLLM:
    stub = _StubLLM(responses)

    def _factory(temperature=0.0, max_tokens=1024, **overrides):
        return stub

    monkeypatch.setattr(
        "apecx_integration.composition.steps.multi_sample_drafter_step.build_chat_llm",
        _factory,
    )
    return stub


def _stage(tmp_path: Path, *, yaml_extras: str = "") -> MultiSampleDrafterStep:
    p = tmp_path / "v.yml"
    p.write_text("name: multi_drafter_test\ntemperature: 0.5\n" + yaml_extras)
    return MultiSampleDrafterStep.from_config(str(p))


def test_loads(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "multi_drafter_test"


def test_temp0_with_n_gt_1_rejected(tmp_path):
    """FAIL-FAST: temp=0 with N>1 produces identical samples."""
    with pytest.raises(Exception, match="(?i)temperature|identical"):
        _stage(tmp_path, yaml_extras="temperature: 0.0\nn_samples: 3\n")


def test_empty_spec_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, ["irrelevant"])
    with pytest.raises(ValueError, match="empty code_spec"):
        asyncio.run(step.process({"code_spec": "  "}))


def test_emits_n_candidates(tmp_path, monkeypatch):
    step = _stage(tmp_path, yaml_extras="n_samples: 3\n")
    _patch_llm(
        monkeypatch,
        [
            "```python\ndef f(): return 1\n```",
            "```python\ndef f(): return 2\n```",
            "```python\ndef f(): return 3\n```",
        ],
    )
    out = asyncio.run(step.process({"code_spec": "Write f"}))
    assert len(out["candidates"]) == 3
    sources = {c["code_source"] for c in out["candidates"]}
    assert len(sources) == 3, "expected 3 distinct candidates"


def test_drops_empty_samples(tmp_path, monkeypatch):
    step = _stage(tmp_path, yaml_extras="n_samples: 3\n")
    _patch_llm(
        monkeypatch,
        ["```python\ndef f(): return 1\n```", "", "```python\ndef f(): return 3\n```"],
    )
    out = asyncio.run(step.process({"code_spec": "Write f"}))
    assert len(out["candidates"]) == 2


def test_all_empty_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path, yaml_extras="n_samples: 3\n")
    _patch_llm(monkeypatch, ["", "", ""])
    with pytest.raises(ValueError, match="all .* samples returned empty"):
        asyncio.run(step.process({"code_spec": "Write f"}))


def test_output_schema(tmp_path, monkeypatch):
    step = _stage(tmp_path, yaml_extras="n_samples: 2\n")
    _patch_llm(monkeypatch, ["```python\ndef a(): pass\n```", "```python\ndef b(): pass\n```"])
    out = asyncio.run(
        step.process(
            {
                "code_spec": "Write f",
                "entry_point": "f",
                "test_hint": "assert f() == 1",
            }
        )
    )
    assert "candidates" in out
    assert "n_samples" in out
    assert "temperature" in out
    assert out["code_spec"] == "Write f"
    assert out["entry_point"] == "f"
    assert out["test_hint"] == "assert f() == 1"


def test_task_category_passthrough(tmp_path, monkeypatch):
    # Regression pin: integrated workflow depends on task_category
    # surviving the drafter fan-out -> aggregator fan-in -> recorder
    # chain. A silent drop here defeats the per-category memory bucket.
    step = _stage(tmp_path, yaml_extras="n_samples: 1\n")
    _patch_llm(monkeypatch, ["```python\ndef a(): pass\n```"])
    out = asyncio.run(step.process({"code_spec": "Write a step", "task_category": "step"}))
    assert out["task_category"] == "step"
