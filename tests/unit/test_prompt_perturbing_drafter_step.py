"""Unit tests for PromptPerturbingDrafterStep (strong-form SGDe drafter).

Coverage targets:
* config loading via from_config (no direct-ctor; FAIL-FAST surface)
* perturbation set validation (>=2, no duplicates, no empty)
* n_samples wraparound rule (>=len(perturbations) + T=0 -> reject)
* output schema (drop-in for ConsensusAggregatorStep)
* per-sample stem appears in the user message (perturbation actually applied)
* task_category passthrough (integrated-workflow requirement)
* trigger-envelope unwrap
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.prompt_perturbing_drafter_step import (
    PromptPerturbingDrafterStep,
)


class _CapturingStubLLM:
    """Records each invoke()'s system + user messages so we can prove the
    perturbation was actually applied to the user-message stem."""

    def __init__(self, responses: list[str]):
        self._responses = responses
        self._call_count = 0
        self.invocations: list[tuple[str, str]] = []  # (system, user)

    def invoke(self, messages):
        idx = self._call_count
        self._call_count += 1
        sys_msg = next((m.content for m in messages if m.__class__.__name__ == "SystemMessage"), "")
        user_msg = next((m.content for m in messages if m.__class__.__name__ == "HumanMessage"), "")
        self.invocations.append((sys_msg, user_msg))
        text = self._responses[idx % len(self._responses)]

        class _R:
            content = text

        return _R()


def _patch_llm(monkeypatch, responses: list[str]) -> _CapturingStubLLM:
    stub = _CapturingStubLLM(responses)

    def _factory(temperature=0.0, max_tokens=1024, **overrides):
        return stub

    monkeypatch.setattr(
        "apecx_integration.composition.steps.prompt_perturbing_drafter_step.build_chat_llm",
        _factory,
    )
    return stub


def _stage(tmp_path: Path, *, yaml_extras: str = "") -> PromptPerturbingDrafterStep:
    p = tmp_path / "v.yml"
    p.write_text("name: ppd_test\n" + yaml_extras)
    return PromptPerturbingDrafterStep.from_config(str(p))


def test_loads_with_defaults(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "ppd_test"
    # Defaults: ["Implement", "Author", "Write"], n_samples=0 -> 3, T=0.0
    assert len(step.perturbations) == 3


def test_too_few_perturbations_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)perturbation"):
        _stage(tmp_path, yaml_extras="perturbations:\n  - Implement\n")


def test_duplicate_perturbations_rejected(tmp_path):
    """Duplicates defeat the variance purpose — FAIL-FAST."""
    with pytest.raises(Exception, match="(?i)duplicate"):
        _stage(
            tmp_path,
            yaml_extras="perturbations:\n  - Implement\n  - implement\n",
        )


def test_empty_perturbation_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)non-empty"):
        _stage(tmp_path, yaml_extras="perturbations:\n  - Implement\n  - '  '\n")


def test_wraparound_with_t0_rejected(tmp_path):
    """n_samples > len(perturbations) AND T=0 → identical wraparound = silent
    failure. Must FAIL-FAST."""
    with pytest.raises(Exception, match="(?i)wraparound|temperature"):
        _stage(
            tmp_path,
            yaml_extras=("perturbations:\n  - A\n  - B\nn_samples: 5\ntemperature: 0.0\n"),
        )


def test_wraparound_with_temp_above_zero_allowed(tmp_path):
    """Same shape but T > 0 — wraparound becomes meaningful, gate opens."""
    step = _stage(
        tmp_path,
        yaml_extras=("perturbations:\n  - A\n  - B\nn_samples: 4\ntemperature: 0.3\n"),
    )
    assert len(step.perturbations) == 2


def test_empty_spec_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, ["irrelevant"])
    with pytest.raises(ValueError, match="empty code_spec"):
        asyncio.run(step.process({"code_spec": "  "}))


def test_perturbations_appear_in_user_messages(tmp_path, monkeypatch):
    """The actual mechanism: each sample's user message should start with
    its assigned stem phrase. If this regresses, perturbation has no
    effect and the step becomes a degenerate multi-sample drafter."""
    step = _stage(
        tmp_path,
        yaml_extras=("perturbations:\n  - Implement\n  - Author\n  - Write\n"),
    )
    stub = _patch_llm(
        monkeypatch,
        [
            "```python\ndef f(): return 1\n```",
            "```python\ndef f(): return 2\n```",
            "```python\ndef f(): return 3\n```",
        ],
    )
    asyncio.run(step.process({"code_spec": "compute the answer"}))
    assert len(stub.invocations) == 3
    user_msgs = [u for _, u in stub.invocations]
    assert any(u.startswith("Implement the following:") for u in user_msgs), user_msgs
    assert any(u.startswith("Author the following:") for u in user_msgs), user_msgs
    assert any(u.startswith("Write the following:") for u in user_msgs), user_msgs


def test_emits_n_distinct_candidates(tmp_path, monkeypatch):
    step = _stage(tmp_path)
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
    # Each candidate carries its perturbation tag.
    for c in out["candidates"]:
        assert "code_source" in c
        assert "perturbation" in c
    seen = {c["perturbation"] for c in out["candidates"]}
    assert seen == {"Implement", "Author", "Write"}


def test_drops_empty_samples(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(
        monkeypatch,
        ["```python\ndef f(): return 1\n```", "", "```python\ndef g(): return 3\n```"],
    )
    out = asyncio.run(step.process({"code_spec": "Write f"}))
    assert len(out["candidates"]) == 2


def test_all_empty_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, ["", "", ""])
    with pytest.raises(ValueError, match="all .* samples returned empty"):
        asyncio.run(step.process({"code_spec": "Write f"}))


def test_output_schema_drop_in_with_aggregator(tmp_path, monkeypatch):
    """The aggregator consumes candidates[*].code_source; verify the
    output shape is drop-in compatible with MultiSampleDrafterStep."""
    step = _stage(tmp_path)
    _patch_llm(
        monkeypatch,
        [
            "```python\ndef a(): pass\n```",
            "```python\ndef b(): pass\n```",
            "```python\ndef c(): pass\n```",
        ],
    )
    out = asyncio.run(
        step.process(
            {
                "code_spec": "Write f",
                "entry_point": "f",
                "test_hint": "assert f() == 1",
                "task_category": "step",
            }
        )
    )
    assert "candidates" in out
    assert "n_samples" in out
    assert "temperature" in out
    assert "model" in out
    assert "perturbations_used" in out
    assert out["code_spec"] == "Write f"
    assert out["entry_point"] == "f"
    assert out["test_hint"] == "assert f() == 1"
    assert out["task_category"] == "step"


def test_trigger_envelope_unwrap(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(
        monkeypatch,
        [
            "```python\ndef f(): pass\n```",
            "```python\ndef f(): pass\n```",
            "```python\ndef f(): pass\n```",
        ],
    )
    out = asyncio.run(
        step.process({"ppd_input": {"code_spec": "Write f", "task_category": "tool"}})
    )
    assert out["code_spec"] == "Write f"
    assert out["task_category"] == "tool"
