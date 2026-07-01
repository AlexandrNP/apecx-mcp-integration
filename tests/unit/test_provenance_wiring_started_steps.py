"""#3 (2026-07-01) — summarize_run surfaces a step that STARTED but produced no completion record.

A step that hangs past the cascade timeout (or fails before recording) emits a G37 ``step_start``
event but no G4 record, so the record-only fold silently dropped it — a ``cascade_timeout`` run
summary omitted the very step that hung. These pin that such a step now appears in
``RunSummary.steps`` (records still win when present; this only adds the missing ones).
"""

from __future__ import annotations

from nanobrain.core.step_events import StepEvent

from apecx_integration.composition.runtime.provenance_wiring import (
    ProvenanceRun,
    summarize_run,
)


def _ev(event_type: str, step_name: str, **payload) -> StepEvent:
    return StepEvent(
        event_type=event_type,
        step_name=step_name,
        run_id=None,
        timestamp_iso="2026-07-01T00:00:00Z",
        payload=dict(payload),
    )


def _rec(step_name: str) -> dict:
    return {"step_name": step_name, "tool_calls": [], "llm_calls": []}


def test_summarize_run_includes_started_no_record_step():
    # A completed step (has a record), then a step that STARTED but never completed (hung at the
    # cascade timeout) -> no record. The hung step must still appear, named, with status 'started'.
    prov = ProvenanceRun(
        result={"status": "cascade_timeout"},
        step_records=[_rec("A")],
        step_events=[
            _ev("step_start", "A"),
            _ev("step_complete", "A", duration_seconds=1.0),
            _ev("step_start", "B"),  # started, never completed -> no record
        ],
    )
    summary = summarize_run(prov)
    by_name = {s.step_name: s for s in summary.steps}
    assert by_name["A"].status == "completed"
    assert "B" in by_name, "in-flight step B was dropped from the summary"
    assert by_name["B"].status == "started"


def test_summarize_run_includes_failed_no_record_step():
    prov = ProvenanceRun(
        result={"status": "completed"},
        step_records=[],
        step_events=[
            _ev("step_start", "X"),
            _ev(
                "step_failed",
                "X",
                duration_seconds=0.2,
                exception={"type": "ValueError", "message": "boom"},
            ),
        ],
    )
    summary = summarize_run(prov)
    by_name = {s.step_name: s for s in summary.steps}
    assert "X" in by_name
    assert by_name["X"].status == "failed"


def test_summarize_run_completion_records_unchanged():
    # Regression: a fully-recorded run is unaffected — records still produce the rows, in order,
    # and no phantom event-only rows are appended.
    prov = ProvenanceRun(
        result={"status": "completed"},
        step_records=[_rec("A"), _rec("B")],
        step_events=[
            _ev("step_start", "A"),
            _ev("step_complete", "A"),
            _ev("step_start", "B"),
            _ev("step_complete", "B"),
        ],
    )
    summary = summarize_run(prov)
    assert [s.step_name for s in summary.steps] == ["A", "B"]
    assert all(s.status == "completed" for s in summary.steps)
