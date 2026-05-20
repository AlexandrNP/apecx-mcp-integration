"""Observed workflow run (EO-03 core): run a workflow and return the LLM-facing envelope
plus the visibility summary in one call.

Composes the built pieces — ``run_with_provenance`` (G4/G37 capture) + ``summarize_run``
(the "what ran" view) + ``WorkflowResult`` extraction from the workflow output. This is the
core the MCP ``run_workflow`` tool wraps (the tool adds workflow-name lookup + FastMCP
registration on top).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from apecx_integration.composition.runtime.provenance_wiring import (
    RunSummary,
    run_with_provenance,
    summarize_run,
)
from apecx_integration.composition.schemas.workflow_result import WorkflowResult


@dataclass
class WorkflowRunOutcome:
    """Everything a caller needs after a run: raw output, the LLM-facing envelope, the summary."""

    raw_result: dict[str, Any]
    workflow_result: WorkflowResult | None
    run_summary: RunSummary


def _extract_workflow_result(raw: dict[str, Any]) -> WorkflowResult | None:
    """Find a WorkflowResult-shaped value in the workflow output, if the workflow emitted one.

    Returns None when no output validates as a WorkflowResult — a workflow is not required to
    emit the envelope (only those ending in an EnvelopeStep do). None is the honest answer, not
    a silent empty envelope.
    """
    if not isinstance(raw, dict):
        return None
    for key, value in raw.items():
        if key == "status" or not isinstance(value, dict):
            continue
        try:
            return WorkflowResult.model_validate(value)
        except ValidationError:
            continue
    return None


async def run_workflow_observed(
    workflow: Any,
    input_data: dict[str, Any],
    *,
    redact: list[str] | None = None,
    **run_kwargs: Any,
) -> WorkflowRunOutcome:
    """Run a workflow with provenance + events captured; return envelope + run summary."""
    prov_run = await run_with_provenance(workflow, input_data, redact=redact, **run_kwargs)
    return WorkflowRunOutcome(
        raw_result=prov_run.result,
        workflow_result=_extract_workflow_result(prov_run.result),
        run_summary=summarize_run(prov_run),
    )
