"""CW-SM1 — unit tests for WorkflowSummarizerStep.

LLM-mocked. Pins:
  1. Loads via from_config; default prompt has required-sections cue.
  2. Missing 'analysis' key raises.
  3. Analysis without required keys raises.
  4. LLM empty response → ValueError.
  5. LLM response missing required sections → ValueError (default).
  6. require_all_sections=False allows responses without all sections.
  7. Happy path returns summary_markdown + raw_response.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.workflow_summarizer_step import (
    WorkflowSummarizerStep,
)


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
        "apecx_integration.composition.steps.workflow_summarizer_step.build_chat_llm",
        _factory,
    )


def _stage_step(tmp_path: Path, *, yaml_extras: str = "") -> WorkflowSummarizerStep:
    body = "name: summarizer_test\n" + yaml_extras
    p = tmp_path / "summarizer.yml"
    p.write_text(body)
    return WorkflowSummarizerStep.from_config(str(p))


_VALID_ANALYSIS = {
    "workflow_name": "demo",
    "description": "Demo workflow.",
    "config_version": 2,
    "input_data_units": ["workflow_input"],
    "output_data_units": ["workflow_output"],
    "steps": [
        {
            "step_name": "a",
            "class": "pkg.A",
            "has_config_path": True,
            "input_data_unit_names": [],
            "output_data_unit_names": [],
            "trigger_classes": [],
        }
    ],
    "links": [],
    "topology_summary": "single-step workflow (a)",
    "issues": [],
    "summary_line": "Workflow 'demo': 1 step(s), 0 link(s), 0 issue(s).",
}


_FULL_MARKDOWN = (
    "## What this workflow does\n"
    "A demo workflow with one step.\n\n"
    "## Steps\n"
    "- **a** (A): the only step.\n\n"
    "## Data flow\n"
    "Input flows into a; a produces the output.\n\n"
    "## Issues to know about\n"
    "No structural issues detected.\n\n"
    "## Honest caveats\n"
    "Analysis doesn't introspect step bodies; runtime behavior may differ.\n"
)


def test_loads_with_default_prompt(tmp_path):
    step = _stage_step(tmp_path)
    assert step.name == "summarizer_test"
    assert "OUTPUT RULES" in step.system_prompt or "Markdown" in step.system_prompt


def test_missing_analysis_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, _FULL_MARKDOWN)
    with pytest.raises(ValueError, match="analysis"):
        asyncio.run(step.process({}))


def test_analysis_missing_required_keys_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, _FULL_MARKDOWN)
    with pytest.raises(ValueError, match="missing required key"):
        asyncio.run(step.process({"analysis": {"workflow_name": "x"}}))


def test_empty_llm_response_raises(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, "   ")
    with pytest.raises(ValueError, match="empty response"):
        asyncio.run(step.process({"analysis": _VALID_ANALYSIS}))


def test_response_missing_sections_raises_default(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(
        monkeypatch,
        "## What this workflow does\nbody.\n\n## Steps\nbullet.\n",
    )
    with pytest.raises(ValueError, match="missing required sections"):
        asyncio.run(step.process({"analysis": _VALID_ANALYSIS}))


def test_require_all_sections_false_admits_partial_response(tmp_path, monkeypatch):
    step = _stage_step(tmp_path, yaml_extras="require_all_sections: false\n")
    _patch_llm(monkeypatch, "## What this workflow does\nshort response.\n")
    result = asyncio.run(step.process({"analysis": _VALID_ANALYSIS}))
    assert "short response" in result["summary_markdown"]


def test_happy_path_returns_full_summary(tmp_path, monkeypatch):
    step = _stage_step(tmp_path)
    _patch_llm(monkeypatch, _FULL_MARKDOWN)
    result = asyncio.run(step.process({"analysis": _VALID_ANALYSIS}))
    for section in (
        "## What this workflow does",
        "## Steps",
        "## Data flow",
        "## Issues to know about",
        "## Honest caveats",
    ):
        assert section in result["summary_markdown"]
    assert "raw_response" in result
