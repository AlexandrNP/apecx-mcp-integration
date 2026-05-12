"""CW-TW1 — unit tests for TestWriteStep.

Pins:
  1. Loads via from_config; default prompt.
  2. Empty / non-string code_source raises (EMPTY-FAIL).
  3. LLM emits prose-only → AST gate raises.
  4. LLM emits parseable Python with NO test_* function → min_tests gate raises.
  5. min_tests=2 requires ≥2 test functions; 1-test response raises.
  6. Happy path: at least one test_* function → returns dict with passthrough.
  7. Fence-strip removes ```python fences once.
  8. Passthrough fields (code_source, code_spec, function_name) present in output.

LLM mocked. Real-Ollama coverage in CW-T2 integration tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.test_write_step import TestWriteStep


class _StubLLM:
    def __init__(self, text: str):
        self._text = text

    def invoke(self, _messages):
        class _R:
            content = self._text

        return _R()


def _patch_llm(monkeypatch, text: str):
    def _factory(**_kw):
        return _StubLLM(text)

    monkeypatch.setattr(
        "apecx_integration.composition.steps.test_write_step.build_chat_llm",
        _factory,
    )


def _stage_step(tmp_path: Path, *, yaml_extras: str = "") -> TestWriteStep:
    body = "name: test_writer\n" + yaml_extras
    p = tmp_path / "test_writer.yml"
    p.write_text(body)
    return TestWriteStep.from_config(str(p))


def test_loads_with_default_prompt(tmp_path):
    step = _stage_step(tmp_path)
    assert step.name == "test_writer"
    assert "OUTPUT RULES" in step.system_prompt


def test_empty_code_source_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "irrelevant")
    with pytest.raises(ValueError, match="code_source"):
        asyncio.run(step.process({"code_source": "  ", "code_spec": "x"}))


def test_non_dict_input_raises(tmp_path):
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process("not a dict"))


def test_prose_only_response_raises_via_ast_gate(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "Sure! Here is a paragraph of explanation.")
    with pytest.raises(ValueError, match="not valid Python"):
        asyncio.run(step.process({"code_source": "def f(): return 1", "code_spec": "f"}))


def test_no_test_functions_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        "def helper():\n    return 1\n\nx = helper()\n",
    )
    with pytest.raises(ValueError, match="test_\\*"):
        asyncio.run(step.process({"code_source": "def f(): return 1", "code_spec": "x"}))


def test_min_tests_2_one_function_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path, yaml_extras="min_tests: 2\n")
    _patch_llm(monkeypatch, "def test_one():\n    assert 1 == 1\n")
    with pytest.raises(ValueError, match="expected at least"):
        asyncio.run(step.process({"code_source": "def f(): return 1", "code_spec": "x"}))


def test_happy_path_returns_test_code_with_passthrough(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    src = (
        "def test_happy():\n    assert add(2, 3) == 5\n\n"
        "def test_negative():\n    assert add(-1, 1) == 0\n"
    )
    _patch_llm(monkeypatch, src)
    code_src = "def add(a: int, b: int) -> int:\n    return a + b\n"
    result = asyncio.run(
        step.process(
            {
                "code_source": code_src,
                "code_spec": "Add two integers.",
                "function_name": "add",
            }
        )
    )
    assert result["test_function_count"] == 2
    assert "test_happy" in result["test_code"]
    # Passthrough fields:
    assert result["code_source"] == code_src
    assert result["code_spec"] == "Add two integers."
    assert result["function_name"] == "add"


def test_fence_stripped_once(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        "```python\ndef test_x():\n    assert 1\n```\n",
    )
    result = asyncio.run(step.process({"code_source": "def x(): return 1", "code_spec": "x"}))
    assert "```" not in result["test_code"]
    assert result["test_function_count"] == 1
