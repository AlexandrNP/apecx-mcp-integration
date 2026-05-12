"""CW-2 — unit tests for CodeWriteStep.

Pins:
  1. Loads via from_config with bundled default prompt.
  2. Empty/missing code_spec raises ValueError.
  3. AST gate rejects unparseable LLM output (prose-only response).
  4. Fence-stripping removes ```python ... ``` ONCE; raises if still
     unparseable after the strip (no "try multiple parses" drift mask).
  5. Function-name verification: required name not defined → raise.
  6. Function-name verification: required name found → return.
  7. require_function_name=False skips the name check.
  8. ``extra='forbid'`` rejects YAML typos at load.
  9. Custom system_prompt_file resolves against the YAML directory.

LLM mocked via monkeypatch on ``build_chat_llm`` — no real LLM call
in unit tests. The full LLM round-trip is covered by CW-11 against
real Ollama.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.code_write_step import (
    CodeWriteStep,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _StubLLM:
    """Minimal LangChain-Chat-compatible stub."""

    def __init__(self, response_text: str):
        self._response_text = response_text

    def invoke(self, _messages):
        class _R:
            content = self._response_text

        return _R()


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    """Replace build_chat_llm with a function returning a stub LLM
    that always returns ``response_text``."""

    def _factory(temperature=0.0, max_tokens=1024, **overrides):
        return _StubLLM(response_text)

    monkeypatch.setattr(
        "apecx_integration.composition.steps.code_write_step.build_chat_llm",
        _factory,
    )


def _stage_step(
    tmp_path: Path,
    *,
    yaml_extras: str = "",
    yaml_name: str = "code_write_step.yml",
) -> CodeWriteStep:
    body = "name: code_write_test\n" + yaml_extras
    p = tmp_path / yaml_name
    p.write_text(body)
    return CodeWriteStep.from_config(str(p))


# ---------------------------------------------------------------------------
# 1-2. Loading + input gates
# ---------------------------------------------------------------------------


def test_loads_with_default_prompt(tmp_path):
    step = _stage_step(tmp_path)
    assert step.name == "code_write_test"
    # Default prompt is non-empty and mentions the OUTPUT RULES.
    assert "OUTPUT RULES" in step.system_prompt


def test_empty_code_spec_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "irrelevant — should never be called")
    with pytest.raises(ValueError, match="code_spec"):
        asyncio.run(step.process({"code_spec": "   "}))


def test_missing_code_spec_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "irrelevant")
    with pytest.raises(ValueError, match="code_spec"):
        asyncio.run(step.process({}))


def test_non_dict_input_raises(tmp_path):
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process("not a dict"))


def test_require_function_name_without_name_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)  # require_function_name=True by default
    _patch_llm(monkeypatch, "def foo():\n    return 1\n")
    with pytest.raises(ValueError, match="function_name"):
        asyncio.run(step.process({"code_spec": "write something"}))


# ---------------------------------------------------------------------------
# 3-4. AST gate + fence-stripping
# ---------------------------------------------------------------------------


def test_prose_only_llm_output_raises_via_ast_gate(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        "I cannot do that. As an AI language model, I cannot write code.",
    )
    with pytest.raises(ValueError, match="not valid Python"):
        asyncio.run(step.process({"code_spec": "write fib", "function_name": "fib"}))


def test_fence_stripped_once_when_llm_wraps_output(tmp_path, monkeypatch):
    """The LLM wraps output in ```python ... ``` despite the system
    prompt. We strip once and re-parse; the result should be valid."""
    step = _stage_step(tmp_path)
    response = (
        "```python\n"
        "def fib(n: int) -> int:\n"
        "    if n < 2:\n"
        "        return n\n"
        "    return fib(n - 1) + fib(n - 2)\n"
        "```\n"
    )
    _patch_llm(monkeypatch, response)
    result = asyncio.run(step.process({"code_spec": "fibonacci", "function_name": "fib"}))
    # Fences must be gone.
    assert "```" not in result["code_source"]
    # And the function name is verified.
    assert result["function_name_verified"] == "fib"


def test_unparseable_after_strip_still_raises(tmp_path, monkeypatch):
    """Output that remains unparseable after one fence-strip pass
    must raise — we do NOT try multiple parsing heuristics."""
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        "```python\nthis is not python at all (((\n```\n",
    )
    with pytest.raises(ValueError, match="not valid Python"):
        asyncio.run(step.process({"code_spec": "anything", "function_name": "foo"}))


# ---------------------------------------------------------------------------
# 5-7. Function-name verification
# ---------------------------------------------------------------------------


def test_wrong_function_name_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "def something_else(n):\n    return n\n")
    with pytest.raises(ValueError, match="does not define expected function"):
        asyncio.run(step.process({"code_spec": "do thing", "function_name": "do_thing"}))


def test_correct_function_name_returns(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    src = "def add(a: int, b: int) -> int:\n    return a + b\n"
    _patch_llm(monkeypatch, src)
    result = asyncio.run(step.process({"code_spec": "add two ints", "function_name": "add"}))
    assert result["code_source"].strip() == src.strip()
    assert result["function_name_verified"] == "add"


def test_require_function_name_false_skips_check(tmp_path, monkeypatch):
    step = _stage_step(tmp_path, yaml_extras="require_function_name: false\n")
    # Any parseable source — even without a function — is accepted.
    _patch_llm(monkeypatch, "x = 1 + 2\n")
    result = asyncio.run(step.process({"code_spec": "compute x"}))
    assert result["code_source"].strip() == "x = 1 + 2"
    assert result["function_name_verified"] is None


def test_default_function_name_is_honored(tmp_path, monkeypatch):
    step = _stage_step(tmp_path, yaml_extras="default_function_name: fib\n")
    src = "def fib(n: int) -> int:\n    return n\n"
    _patch_llm(monkeypatch, src)
    result = asyncio.run(step.process({"code_spec": "fibonacci"}))
    assert result["function_name_verified"] == "fib"


# ---------------------------------------------------------------------------
# 8-9. Config validation + prompt resolution
# ---------------------------------------------------------------------------


def test_extra_forbid_rejects_yaml_typos(tmp_path):
    step_path = tmp_path / "step.yml"
    step_path.write_text(
        "name: typo_step\ntemperatur: 0.5\n"  # typo on temperature
    )
    with pytest.raises(Exception) as exc_info:
        CodeWriteStep.from_config(str(step_path))
    # Pydantic surfaces the offending key.
    assert "temperatur" in str(exc_info.value).lower()


def test_custom_system_prompt_file_resolves_against_yaml_dir(tmp_path):
    custom_prompt = tmp_path / "custom_prompt.md"
    custom_prompt.write_text("custom prompt body — different from default")
    step = _stage_step(
        tmp_path,
        yaml_extras="system_prompt_file: custom_prompt.md\nrequire_function_name: false\n",
    )
    assert "custom prompt body" in step.system_prompt
