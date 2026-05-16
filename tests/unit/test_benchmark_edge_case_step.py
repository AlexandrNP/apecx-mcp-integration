"""Unit tests for BenchmarkEdgeCaseStep (pre-drafter edge-case enumerator).

Audit gap closed: prior to this file the step had ZERO unit tests
despite shipping in the MB-1 / F15 scaffold (the one that
catastrophically regressed on MBPP). The lack of coverage was a real
adoption risk — a silent contract regression in the I/O shape would
have gone unnoticed.

Coverage targets:
* config loading via from_config
* FAIL-FAST silent-failure guards (empty spec, empty LLM response,
  non-string LLM content)
* output schema (code_spec enrichment + edge_cases field + passthrough)
* bullet-only filtering of LLM output
* fallback when the LLM produces no bullets
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.benchmark_edge_case_step import (
    BenchmarkEdgeCaseStep,
)


class _StubLLM:
    def __init__(self, content: str):
        self._content = content

    def invoke(self, _messages):
        class _R:
            content = self._content

        return _R()


def _patch_llm(monkeypatch, content: str):
    def _factory(temperature=0.0, max_tokens=1024, **overrides):
        return _StubLLM(content)

    monkeypatch.setattr(
        "apecx_integration.composition.steps.benchmark_edge_case_step.build_chat_llm",
        _factory,
    )


def _stage(tmp_path: Path, *, yaml_extras: str = "") -> BenchmarkEdgeCaseStep:
    p = tmp_path / "v.yml"
    p.write_text("name: edge_case_test\n" + yaml_extras)
    return BenchmarkEdgeCaseStep.from_config(str(p))


def test_loads(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "edge_case_test"


def test_empty_spec_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "irrelevant")
    with pytest.raises(ValueError, match="empty code_spec"):
        asyncio.run(step.process({"code_spec": "  "}))


def test_empty_llm_response_raises(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "")
    with pytest.raises(ValueError, match="empty edge-case list"):
        asyncio.run(step.process({"code_spec": "Reverse a string"}))


def test_output_schema_with_bullets(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    _patch_llm(
        monkeypatch,
        "- empty input\n- single-character string\n- string with only whitespace\n",
    )
    out = asyncio.run(
        step.process(
            {
                "code_spec": "Reverse a string",
                "entry_point": "reverse",
                "test_hint": "assert reverse('abc') == 'cba'",
            }
        )
    )
    # Required output keys.
    for key in ("code_spec", "edge_cases", "entry_point", "test_hint"):
        assert key in out, f"missing output key {key}"
    # Enrichment is in-place on code_spec.
    assert out["code_spec"].startswith("Reverse a string")
    assert "Edge cases to handle:" in out["code_spec"]
    assert "- empty input" in out["edge_cases"]
    assert out["entry_point"] == "reverse"


def test_filters_prose_lines_keeps_bullets(tmp_path, monkeypatch):
    """When the LLM emits both prose AND bullets, only bullets survive."""
    step = _stage(tmp_path)
    _patch_llm(
        monkeypatch,
        "Here are the edge cases:\n- empty list\n- single element\nThat should cover it.\n",
    )
    out = asyncio.run(step.process({"code_spec": "Sort a list"}))
    assert "- empty list" in out["edge_cases"]
    assert "- single element" in out["edge_cases"]
    assert "Here are" not in out["edge_cases"]
    assert "That should" not in out["edge_cases"]


def test_falls_back_to_single_bullet_when_no_dashes(tmp_path, monkeypatch):
    """No bullet lines -> the whole first line gets wrapped as a single bullet."""
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "empty input is the main concern")
    out = asyncio.run(step.process({"code_spec": "Process input"}))
    assert out["edge_cases"].startswith("-")
    assert "empty input" in out["edge_cases"]


def test_strips_think_blocks_from_response(tmp_path, monkeypatch):
    """<think>...</think> sections must not leak into the enriched spec."""
    step = _stage(tmp_path)
    _patch_llm(
        monkeypatch,
        "<think>thinking about cases</think>\n- empty input\n- negative number\n",
    )
    out = asyncio.run(step.process({"code_spec": "Factorial"}))
    assert "<think>" not in out["edge_cases"]
    assert "thinking" not in out["edge_cases"]
    assert "- empty input" in out["edge_cases"]


def test_trigger_envelope_unwrap(tmp_path, monkeypatch):
    """When invoked under a trigger cascade, input may be {unit_name: {...}}.

    The step must unwrap so process() sees the raw payload.
    """
    step = _stage(tmp_path)
    _patch_llm(monkeypatch, "- case A\n- case B\n")
    out = asyncio.run(
        step.process(
            {
                "edge_case_input": {
                    "code_spec": "Foo",
                    "entry_point": "foo",
                }
            }
        )
    )
    assert "case A" in out["edge_cases"]
    assert out["entry_point"] == "foo"
