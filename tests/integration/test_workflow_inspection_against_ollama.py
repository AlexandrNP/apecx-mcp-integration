"""Integration tests for WorkflowAnalysisStep + WorkflowSummarizerStep.

The analyzer is pure-Python (no LLM); we test it against the real
shipped workflows. The summarizer hits real Ollama; auto-skips when
unreachable.

End-to-end flow exercised here:

    workflow YAML path
        ↓
    WorkflowAnalysisStep.process({workflow_path: ...})
        ↓
    analysis dict
        ↓
    WorkflowSummarizerStep.process({analysis: ...})
        ↓
    Markdown summary with 5 required sections
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest

from apecx_integration.composition.steps.workflow_analysis_step import (
    WorkflowAnalysisStep,
)
from apecx_integration.composition.steps.workflow_summarizer_step import (
    WorkflowSummarizerStep,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_WRITING_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing"
)
WRAPPER_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing" / "steps"
)


def _llm_reachable() -> bool:
    base = os.environ.get("APECX_LLM_BASE_URL") or "http://localhost:11434/v1"
    probe = base[:-3] + "/api/tags" if base.endswith("/v1") else base.rstrip("/") + "/api/tags"
    try:
        return httpx.get(probe, timeout=2.0).status_code == 200
    except Exception:
        return False


SKIP_LLM = "LLM not reachable — set APECX_LLM_BASE_URL"


def test_analyzer_round_trips_against_real_workflow(tmp_path):
    """Pure-Python — no LLM. Pin against the shipped reflection
    workflow YAML to catch any drift in the analyzer's output shape."""
    step_path = tmp_path / "analyzer.yml"
    step_path.write_text("name: real_test\n")
    step = WorkflowAnalysisStep.from_config(str(step_path))

    workflow_yml = CODE_WRITING_DIR / "code_reflection_workflow.yml"
    result = asyncio.run(step.process({"workflow_path": str(workflow_yml)}))

    assert result["workflow_name"] == "code_reflection_workflow"
    assert result["config_version"] == 2
    assert len(result["steps"]) == 2
    assert len(result["links"]) == 5
    # All links auto_transfer:true.
    assert all(link["auto_transfer"] for link in result["links"])
    # No issues on a known-good workflow.
    assert result["issues"] == []


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_summarizer_against_real_llm(tmp_path):
    """Wire the analyzer's output into the summarizer; verify the
    Markdown response has all 5 required sections."""
    # Stage both steps from the same tmp_path-based YAMLs.
    analyzer_yml = tmp_path / "analyzer.yml"
    analyzer_yml.write_text("name: analyzer\n")
    analyzer = WorkflowAnalysisStep.from_config(str(analyzer_yml))

    summarizer_yml = tmp_path / "summarizer.yml"
    summarizer_yml.write_text("name: summarizer\ntemperature: 0.0\nmax_tokens: 1024\n")
    summarizer = WorkflowSummarizerStep.from_config(str(summarizer_yml))

    workflow_yml = CODE_WRITING_DIR / "code_reflection_workflow.yml"
    analysis = asyncio.run(analyzer.process({"workflow_path": str(workflow_yml)}))

    start = time.monotonic()
    summary = asyncio.run(summarizer.process({"analysis": analysis}))
    elapsed = time.monotonic() - start

    assert elapsed < 120.0
    md = summary["summary_markdown"]
    for section in (
        "## What this workflow does",
        "## Steps",
        "## Data flow",
        "## Issues to know about",
        "## Honest caveats",
    ):
        assert section in md, (
            f"missing section {section!r} in summary; first 200 chars: {md[:200]!r}"
        )
    print(
        f"\n[summarizer] elapsed={elapsed:.2f}s; length={len(md)} chars; section count check passed"
    )
