"""Provenance + step-event wiring around a workflow run (EO-40/41/42/43).

nanobrain ships G4 ``ProvenanceContext`` (per-step durable records) and G37 step events
(in-process live stream). ``BaseStep._execute_process`` auto-records to BOTH when a context is
active (verified in ``nanobrain/core/step.py``: ``record_step_invocation`` at l.2032/2080,
``publish_step_event`` at l.2010/2051/2093). Neither is auto-activated by ``Workflow.run`` — so
this module is the activation seam: run a workflow with provenance + events captured, then
derive a scientist-facing run summary (effective per-step tools/params).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanobrain.core.provenance import ProvenanceContext, ProvenanceSinkBase
from nanobrain.core.step_events import StepEvent, subscribe_to_step_events
from pydantic import BaseModel, ConfigDict


class MemorySink(ProvenanceSinkBase):
    """In-memory provenance sink — collects records for programmatic inspection.

    The built-in ``JsonlSink`` only writes to disk; this captures records in process so a
    caller can read them straight back after a run without a temp file.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def write_record(self, record: dict[str, Any]) -> None:
        self.records.append(record)

    async def flush(self) -> None:
        return None


@dataclass
class ProvenanceRun:
    """A workflow run plus everything captured about it."""

    result: dict[str, Any]
    step_records: list[dict[str, Any]] = field(default_factory=list)
    step_events: list[StepEvent] = field(default_factory=list)


async def run_with_provenance(
    workflow: Any,
    input_data: dict[str, Any],
    *,
    redact: list[str] | None = None,
    **run_kwargs: Any,
) -> ProvenanceRun:
    """Run a workflow with G4 provenance + G37 events captured.

    ``redact=None`` uses nanobrain's default (``prompts`` + ``executor_env`` elided — the
    sensible secure default for a scientist-facing summary). Pass an explicit list to override;
    ``[]`` captures everything (trusted local debugging only).

    Uses ``workflow.run`` (NOT manual ``process()`` + ``wait_for_cascade``) so the cascade
    fully drains and every step's auto-recorded provenance lands before records are read
    (G124/G125).
    """
    sink = MemorySink()
    config: dict[str, Any] = {} if redact is None else {"redact": redact}
    prov = ProvenanceContext.from_config(config, sink=sink)
    events: list[StepEvent] = []
    with prov.activate(), subscribe_to_step_events(events.append):
        result = await workflow.run(input_data, **run_kwargs)
    await prov.flush()
    return ProvenanceRun(
        result=result,
        step_records=list(sink.records),
        step_events=list(events),
    )


class StepRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_name: str
    status: str
    duration_seconds: float | None = None
    n_tool_calls: int = 0
    n_llm_calls: int = 0


class RunSummary(BaseModel):
    """Scientist-facing 'what ran' view derived from G4 records + G37 events (EO-42)."""

    model_config = ConfigDict(extra="forbid")

    workflow_status: str | None = None
    steps: list[StepRunSummary]


def summarize_run(prov_run: ProvenanceRun) -> RunSummary:
    """Fold the per-step records + events into a flat run summary."""
    status_by_step: dict[str, str] = {}
    duration_by_step: dict[str, float | None] = {}
    for ev in prov_run.step_events:
        if ev.event_type == "step_complete":
            status_by_step[ev.step_name] = "completed"
            duration_by_step[ev.step_name] = ev.payload.get("duration_seconds")
        elif ev.event_type == "step_failed":
            status_by_step[ev.step_name] = "failed"
            duration_by_step[ev.step_name] = ev.payload.get("duration_seconds")
        else:
            status_by_step.setdefault(ev.step_name, "started")

    steps: list[StepRunSummary] = []
    for rec in prov_run.step_records:
        name = str(rec.get("step_name", "?"))
        steps.append(
            StepRunSummary(
                step_name=name,
                status=status_by_step.get(name, "unknown"),
                duration_seconds=duration_by_step.get(name),
                n_tool_calls=len(rec.get("tool_calls") or []),
                n_llm_calls=len(rec.get("llm_calls") or []),
            )
        )
    workflow_status = prov_run.result.get("status") if isinstance(prov_run.result, dict) else None
    return RunSummary(workflow_status=workflow_status, steps=steps)
