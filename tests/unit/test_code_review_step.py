"""CW-3 — unit tests for CodeReviewStep.

Pins:
  1. Loads via from_config; default prompt body.
  2. Missing/empty code_source or code_spec raises.
  3. LLM emitting prose-only (no JSON) raises.
  4. LLM emitting malformed JSON raises.
  5. LLM emitting JSON with wrong shape raises.
  6. Grounded-rejection gate: approved=false + empty concerns → raise.
  7. Happy path: approved=true returns structured verdict.
  8. Happy path: approved=false with concerns returns verdict.

LLM mocked. Real-Ollama coverage in CW-11.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from apecx_integration.composition.steps.code_review_step import (
    CodeReviewStep,
)


class _StubLLM:
    def __init__(self, response_text: str):
        self._response_text = response_text

    def invoke(self, _messages):
        class _R:
            content = self._response_text

        return _R()


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    def _factory(temperature=0.0, max_tokens=768, **overrides):
        return _StubLLM(response_text)

    monkeypatch.setattr(
        "apecx_integration.composition.steps.code_review_step.build_chat_llm",
        _factory,
    )


def _stage_step(tmp_path: Path, *, yaml_extras: str = "") -> CodeReviewStep:
    body = "name: code_review_test\n" + yaml_extras
    p = tmp_path / "review.yml"
    p.write_text(body)
    return CodeReviewStep.from_config(str(p))


_VALID_CODE = "def fib(n: int) -> int:\n    return n\n"
_VALID_SPEC = "Write a fibonacci function."


def _process(step: CodeReviewStep, **extras):
    payload = {"code_source": _VALID_CODE, "code_spec": _VALID_SPEC}
    payload.update(extras)
    return asyncio.run(step.process(payload))


# ---------------------------------------------------------------------------
# 1-2. Loading + input gates
# ---------------------------------------------------------------------------


def test_loads_with_default_prompt(tmp_path):
    step = _stage_step(tmp_path)
    assert step.name == "code_review_test"
    assert "OUTPUT FORMAT" in step.system_prompt


def test_empty_code_source_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "{}")
    with pytest.raises(ValueError, match="code_source"):
        asyncio.run(step.process({"code_source": "  ", "code_spec": _VALID_SPEC}))


def test_empty_code_spec_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "{}")
    with pytest.raises(ValueError, match="code_spec"):
        asyncio.run(step.process({"code_source": _VALID_CODE, "code_spec": ""}))


def test_non_dict_input_raises(tmp_path):
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process("not a dict"))


# ---------------------------------------------------------------------------
# 3-5. JSON parsing
# ---------------------------------------------------------------------------


def test_prose_only_response_raises(tmp_path, monkeypatch):
    """No JSON envelope in the response → reject. Do NOT default to
    approved=true on parse failure."""
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "The code looks fine to me. No issues.")
    with pytest.raises(ValueError, match="no JSON object"):
        _process(step)


def test_malformed_json_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "{approved: yes, this is not valid json}")
    with pytest.raises(ValueError, match="JSON"):
        _process(step)


def test_wrong_shape_approved_not_bool_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        json.dumps({"approved": "yes", "concerns": [], "suggestions": []}),
    )
    with pytest.raises(ValueError, match="'approved'.*bool"):
        _process(step)


def test_wrong_shape_concerns_not_list_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        json.dumps({"approved": True, "concerns": "should be a list", "suggestions": []}),
    )
    with pytest.raises(ValueError, match="'concerns'.*list"):
        _process(step)


# ---------------------------------------------------------------------------
# 6. Grounded-rejection gate
# ---------------------------------------------------------------------------


def test_grounded_rejection_gate_default_raises_on_empty_concerns(tmp_path, monkeypatch):
    """approved=false WITHOUT concerns is unactionable — the default
    gate must raise."""
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        json.dumps(
            {
                "approved": False,
                "reasoning": "I don't like it.",
                "concerns": [],
                "suggestions": [],
            }
        ),
    )
    with pytest.raises(ValueError, match="approved=false with no concerns"):
        _process(step)


def test_grounded_rejection_gate_opt_in_allows_ungrounded_rejection(tmp_path, monkeypatch):
    step = _stage_step(
        tmp_path,
        yaml_extras="require_concerns_when_rejecting: false\n",
    )
    _patch_llm(
        monkeypatch,
        json.dumps(
            {
                "approved": False,
                "reasoning": "vibes",
                "concerns": [],
                "suggestions": [],
            }
        ),
    )
    verdict = _process(step)
    assert verdict["approved"] is False
    assert verdict["concerns"] == []


# ---------------------------------------------------------------------------
# 7-8. Happy paths
# ---------------------------------------------------------------------------


def test_approved_true_returns_verdict(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        json.dumps(
            {
                "approved": True,
                "reasoning": "Matches spec; returns correct types.",
                "concerns": [],
                "suggestions": ["consider adding base-case test"],
            }
        ),
    )
    verdict = _process(step)
    assert verdict["approved"] is True
    assert verdict["concerns"] == []
    assert verdict["suggestions"] == ["consider adding base-case test"]
    assert "raw_response" in verdict


def test_approved_false_with_concerns_returns_verdict(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        json.dumps(
            {
                "approved": False,
                "reasoning": "Base case wrong.",
                "concerns": ["f(0) returns 0 expected; got n=0 → 0 OK actually"],
                "suggestions": ["pin base case in a test"],
            }
        ),
    )
    verdict = _process(step)
    assert verdict["approved"] is False
    assert len(verdict["concerns"]) == 1
    assert len(verdict["suggestions"]) == 1


def test_llm_emits_extra_keys_are_ignored(tmp_path, monkeypatch):
    """Tolerate extra LLM-emitted keys (some models add 'severity' or
    similar). We don't pass them through but we don't reject either."""
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        json.dumps(
            {
                "approved": True,
                "reasoning": "fine",
                "concerns": [],
                "suggestions": [],
                "severity": "info",  # extra key
            }
        ),
    )
    verdict = _process(step)
    assert "severity" not in verdict
    assert verdict["approved"] is True


def test_llm_response_with_leading_prose_then_json_parses(tmp_path, monkeypatch):
    """Some models emit 'Sure, here is my review: {...}'. We extract
    the JSON envelope and parse it."""
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        'Sure, here is my review:\n{"approved": true, "reasoning": "ok", "concerns": [], "suggestions": []}',
    )
    verdict = _process(step)
    assert verdict["approved"] is True
