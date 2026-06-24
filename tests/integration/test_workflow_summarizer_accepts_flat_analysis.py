"""WorkflowSummarizerStep accepts a DIRECT (flat) analysis dict, so a direct
WorkflowAnalysisStep -> WorkflowSummarizerStep link works (the composer's natural wiring).

WorkflowAnalysisStep emits the flat analysis ({workflow_name, steps, links, issues, ...}) rather
than wrapping it under 'analysis'; before the fix, summarize required input_data['analysis'] and
raised on the flat form — a latent composer-reliability bug (same class as entity_extraction->
assembly). Tested with a sentinel on _invoke_llm so we assert the analysis was ACCEPTED (reached
the LLM call) without invoking a real LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import apecx_integration

_WRAPPER = (
    Path(apecx_integration.__file__).parent
    / "composition/workflows/code_writing/steps/workflow_summarize.yml"
)
_FLAT = {"workflow_name": "w", "steps": [], "links": [], "issues": []}


class _Marker(Exception):
    pass


def _step():
    from apecx_integration.composition.steps.workflow_summarizer_step import WorkflowSummarizerStep

    return WorkflowSummarizerStep.from_config(str(_WRAPPER))


def test_accepts_flat_analysis_dict(monkeypatch):
    step = _step()
    monkeypatch.setattr(step, "_invoke_llm", lambda **kw: (_ for _ in ()).throw(_Marker()))
    # Flat analysis (the WorkflowAnalysisStep output shape) -> accepted -> reaches the LLM call.
    with pytest.raises(_Marker):
        asyncio.run(step.process(dict(_FLAT)))


def test_wrapped_analysis_still_accepted(monkeypatch):
    step = _step()
    monkeypatch.setattr(step, "_invoke_llm", lambda **kw: (_ for _ in ()).throw(_Marker()))
    with pytest.raises(_Marker):
        asyncio.run(step.process({"analysis": dict(_FLAT)}))


def test_neither_raises_analysis_error():
    step = _step()
    with pytest.raises(ValueError, match="analysis"):
        asyncio.run(step.process({"foo": "bar"}))
