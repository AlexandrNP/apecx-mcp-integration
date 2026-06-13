"""EO thin-surface primitives (EO-03/04/05) — workflows as first-class objects.

These are the §4 "primitives, not super-tools" of ``external_orchestration_design.md``:
the external LLM DISCOVERS workflows (``list_workflows``) and then drives any of them
through this small, generic set rather than through one bespoke tool per task.

- ``run_workflow(name, params)`` — run a catalog workflow by name; return its §5
  ``WorkflowResult`` envelope (markdown + optional data handle/preview) plus a ``run_id``.
- ``inspect_run(run_id)``      — the "what ran" view (per-step status/timing) for a run.
- ``inspect_workflow(name)``   — recursive static structure of a workflow's YAML tree.
- ``apecx_context()``          — session re-orientation: the runs made this session.

Honesty discipline (no silent failure): every gate returns ``{"error": ...}`` with an
actionable message; ``run_workflow`` never reports success on a non-completed run, and a
workflow that completes WITHOUT emitting a ``WorkflowResult`` is wrapped into one whose
markdown says so (its structured output is preserved via a handle, never dropped).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import Context

if TYPE_CHECKING:
    from nanobrain.core.step_events import StepEvent

log = logging.getLogger(__name__)

_SUCCESS_STATUSES = {"completed", "completed_no_await"}

# A streamed stage report: the bundle's {stage, order, markdown, data} fields plus the
# emitting step's identity (step_name, run_id). ``on_stage`` is a SYNCHRONOUS callback —
# the G37 step-event subscriber fires synchronously inside the run.
StageReport = dict[str, Any]
OnStage = Callable[[StageReport], None]
_STAGE_REPORTS_KEY = "stage_reports"


async def run_workflow(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the catalog workflow ``name`` with ``params``; return its result envelope.

    The return is always a ``WorkflowResult``-shaped dict (``markdown`` / ``status`` /
    ``data_handle`` / ``data_preview`` / ``run_id`` / ``error``). Discover valid names +
    their parameters with ``list_workflows`` / ``inspect_workflow``.
    """
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.runtime.observed_run import run_workflow_observed
    from apecx_integration.composition.runtime.run_store import get_run_store
    from apecx_integration.composition.schemas.data_shapes import Bundle
    from apecx_integration.composition.schemas.workflow_result import WorkflowResult
    from apecx_integration.mcp_surface.workflow_registry import (
        _load_workflow_for_entry,
        check_prerequisites,
        load_catalog,
    )

    if not isinstance(name, str) or not name.strip():
        return WorkflowResult.failed(
            error="run_workflow: 'name' must be a non-empty workflow name; "
            "call list_workflows to discover the available workflows."
        ).model_dump(mode="json")
    name = name.strip()
    params = params or {}
    if not isinstance(params, dict):
        return WorkflowResult.failed(
            error=f"run_workflow: 'params' must be an object, got {type(params).__name__}."
        ).model_dump(mode="json")

    catalog = load_catalog()
    entry = next((e for e in catalog.workflows if e.tool_name == name), None)
    if entry is None:
        available = sorted(e.tool_name for e in catalog.workflows)
        return WorkflowResult.failed(
            error=f"run_workflow: unknown workflow {name!r}. "
            f"Available: {available}. Use list_workflows to discover."
        ).model_dump(mode="json")

    met, missing = check_prerequisites(entry.requires)
    if not met:
        return WorkflowResult.failed(
            error=f"run_workflow: workflow {name!r} is unavailable — prerequisites not met: "
            f"{'; '.join(missing)}. Set the missing env vars / install the modules and retry."
        ).model_dump(mode="json")

    try:
        workflow = _load_workflow_for_entry(entry)
    except Exception as exc:  # noqa: BLE001 — surface load failure as a loud envelope
        return WorkflowResult.failed(
            error=f"run_workflow: failed to load workflow {name!r}: {type(exc).__name__}: {exc}"
        ).model_dump(mode="json")

    # RoC-2c — return control to the frontier LLM if required params (per the workflow's OWN
    # step_input_schema) are missing/ill-typed, BEFORE any backend call. This is the param-gap fix:
    # the deterministic side does not guess values — it states exactly what is needed + how to get it.
    from apecx_integration.composition.schemas.control_transfer import missing_param_transfer
    from apecx_integration.mcp_surface.workflow_inputs import (
        derive_required_inputs,
        find_param_gaps,
    )

    gaps = find_param_gaps(params, derive_required_inputs(workflow, entry.input_envelope_key))
    if gaps:
        missing = [g.param_name for g in gaps]
        return WorkflowResult.needs_input(
            missing_param_transfer(gaps),
            markdown=f"`{name}` needs more input before it can run: {missing}.",
        ).model_dump(mode="json")

    if entry.input_envelope_key is not None:
        input_data: dict[str, Any] = {entry.input_envelope_key: dict(params)}
    else:
        input_data = dict(params)

    try:
        outcome = await run_workflow_observed(
            workflow,
            input_data,
            timeout=entry.timeout_seconds,
            settle_ms=entry.settle_ms,
            await_cascade=True,
        )
    except Exception as exc:  # noqa: BLE001 — a run exception is a loud error envelope
        return WorkflowResult.failed(
            error=f"run_workflow: workflow {name!r} raised: {type(exc).__name__}: {exc}"
        ).model_dump(mode="json")

    status = outcome.run_summary.workflow_status
    record = get_run_store().record(
        workflow_name=name,
        status=status,
        run_summary=outcome.run_summary,
        workflow_result=outcome.workflow_result,
    )

    # FAIL-LOUD: a run that did not complete cleanly is an error envelope, never a
    # success with empty data (the G127 "status==completed but outputs empty" trap).
    if status not in _SUCCESS_STATUSES:
        return WorkflowResult.failed(
            error=f"workflow {name!r} did not complete cleanly (status={status!r}). "
            f"Call inspect_run({record.run_id!r}) for the per-step breakdown.",
            run_id=record.run_id,
        ).model_dump(mode="json")

    # The workflow emitted a standard envelope (it ends in an EnvelopeStep): use it.
    if outcome.workflow_result is not None:
        updates: dict[str, Any] = {"run_id": record.run_id}
        # E3-8: the per-run provenance record's run_id is a named null at collection time
        # (the run id is not known until the run completes) — stamp it here, in lock-step
        # with the envelope's own run_id, so the provenance is self-contained.
        prov = outcome.workflow_result.provenance
        if isinstance(prov, dict):
            updates["provenance"] = {**prov, "run_id": record.run_id}
        stamped = outcome.workflow_result.model_copy(update=updates)
        return stamped.model_dump(mode="json")

    # The workflow completed but emits no standard envelope (legacy workflow without an
    # EnvelopeStep). Do NOT drop its output: stash the raw output behind a handle and
    # point the LLM at it, and say plainly that no markdown narrative was produced.
    raw_outputs = (
        {k: v for k, v in outcome.raw_result.items() if k != "status"}
        if isinstance(outcome.raw_result, dict)
        else {"value": outcome.raw_result}
    )
    bundle = Bundle(parts=raw_outputs)
    handle = default_handle_store().put(bundle)
    return WorkflowResult(
        markdown=(
            f"`{name}` completed but does not emit a standard result envelope "
            f"(no EnvelopeStep). Its structured output is attached as `data_handle`; "
            f"output keys: {sorted(raw_outputs.keys())}."
        ),
        data_handle=handle,
        data_preview=bundle.preview(),
        run_id=record.run_id,
    ).model_dump(mode="json")


def _make_stage_streamer(on_stage: OnStage) -> Callable[[StepEvent], None]:
    """Build a G37 step-event subscriber that extracts NEWLY-added stage reports.

    Each reasoning stage appends one ``{stage, order, markdown, data}`` entry to the
    bundle's ``stage_reports`` list (``composition/steps/_stage_report.py``); the list
    accumulates step-to-step, so every ``step_complete`` event carries the FULL list so
    far. The subscriber diffs against what it has already seen by ``(stage, order)`` and
    invokes ``on_stage`` once per new report, IN ARRIVAL ORDER (= step-completion order,
    which is NOT the render ``order`` field).

    RELIABILITY (load-bearing — streaming is observability, not correctness): both the
    extraction and the ``on_stage`` callback are wrapped so NO exception can escape into
    the framework's ``publish_step_event`` loop and perturb the run. A dropped/late stage
    is impossible by construction (each event carries the cumulative list, so a re-delivery
    re-checks ``seen``); a callback failure is caught + LOUDLY logged, never propagated.
    """
    seen: set[tuple[Any, Any]] = set()

    def _subscriber(event: StepEvent) -> None:
        try:
            if event.event_type != "step_complete":
                return
            outputs = event.payload.get("outputs")
            if not isinstance(outputs, dict):
                return
            reports = outputs.get(_STAGE_REPORTS_KEY)
            if not isinstance(reports, list):
                return
            for r in reports:
                if not isinstance(r, dict):
                    continue
                key = (r.get("stage"), r.get("order"))
                if key in seen:
                    continue
                seen.add(key)
                report: StageReport = {
                    "stage": r.get("stage"),
                    "order": r.get("order"),
                    "markdown": r.get("markdown"),
                    "data": r.get("data"),
                    "step_name": event.step_name,
                    "run_id": event.run_id,
                }
                try:
                    on_stage(report)
                except Exception:  # noqa: BLE001 — observability MUST NOT break the run
                    log.exception(
                        "run_workflow_streamed: on_stage callback raised for stage %r "
                        "(step %r) — swallowed; the run completes and returns the same result.",
                        report["stage"],
                        event.step_name,
                    )
        except Exception:  # noqa: BLE001 — an extraction bug MUST NOT break the run either
            log.exception(
                "run_workflow_streamed: stage-report extraction raised for a %r event from "
                "step %r — swallowed.",
                getattr(event, "event_type", "?"),
                getattr(event, "step_name", "?"),
            )

    return _subscriber


async def run_workflow_streamed(
    name: str,
    params: dict[str, Any] | None = None,
    on_stage: OnStage | None = None,
) -> dict[str, Any]:
    """Run the catalog workflow ``name`` EXACTLY like ``run_workflow``, but push each
    reasoning stage's report to ``on_stage`` as the producing step completes.

    The return value is the SAME ``WorkflowResult``-shaped dict ``run_workflow`` returns
    for the same inputs (this function literally ``return``s ``run_workflow``'s value) —
    the headless one-shot output is unchanged. The streamed reports are exactly the stage
    reports that compose the final document's ``### Reasoning trace`` (no divergence: both
    read the same ``stage_reports`` entries).

    Streaming is wired via nanobrain's G37 ``subscribe_to_step_events`` (``step_events.py``):
    the subscriber stacks on top of the provenance subscriber ``run_workflow`` already
    installs, so both observe every event. ``on_stage`` is invoked synchronously inside the
    run; long work should be deferred by the caller (see ``run_workflow_streaming`` for the
    MCP-notification adapter). A ``None`` ``on_stage`` runs the workflow with no streaming.
    """
    from nanobrain.core.step_events import subscribe_to_step_events

    if on_stage is None:
        return await run_workflow(name, params)

    with subscribe_to_step_events(_make_stage_streamer(on_stage)):
        return await run_workflow(name, params)


async def run_workflow_streaming(
    name: str,
    params: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Run a catalog workflow and STREAM each reasoning stage to the MCP client as it
    completes (desktop transport), then return the same result envelope ``run_workflow``
    returns. Use this instead of ``run_workflow`` when a client wants live per-stage
    progress (a desktop UI); a headless client should keep calling ``run_workflow``.

    Per completed stage the client receives two MCP notifications: a progress notification
    (``report_progress`` — increments a counter, ``message`` names the stage) and a
    structured log notification (``send_log_message`` level=info — carries the full stage
    ``{stage, order, markdown, data, step_name, run_id}`` so a desktop pane can render the
    stage's report immediately). Progress notifications no-op cleanly when the client did
    not request them (no ``progressToken``).

    RELIABILITY: streaming is observability, not correctness. Notification failures are
    caught + logged and NEVER change the run — the returned ``WorkflowResult`` is identical
    whether or not the client is listening.
    """
    if ctx is None:
        # No client context to stream to (e.g. a programmatic caller) — run unchanged.
        return await run_workflow(name, params)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[StageReport | object] = asyncio.Queue()
    sentinel = object()

    def _on_stage(report: StageReport) -> None:
        # The G37 subscriber fires synchronously, possibly off the loop thread (a
        # thread-executed step); hand the report to the loop thread. call_soon_threadsafe
        # is safe from the loop thread too. Reports are enqueued IN ARRIVAL ORDER and the
        # single consumer drains them in that order, so notification order == stage order.
        try:
            loop.call_soon_threadsafe(queue.put_nowait, report)
        except Exception:  # noqa: BLE001 — never let an enqueue failure break the run
            log.exception(
                "run_workflow_streaming: failed to enqueue stage %r for notification.",
                report.get("stage"),
            )

    async def _consume() -> None:
        n = 0
        while True:
            item = await queue.get()
            if item is sentinel:
                return
            report: StageReport = item  # type: ignore[assignment]
            n += 1
            try:
                await ctx.report_progress(
                    progress=float(n), message=f"stage complete: {report.get('stage')}"
                )
                await ctx.session.send_log_message(
                    level="info",
                    data={"event": "stage_report", **report},
                    logger="apecx.eo.streaming",
                )
            except Exception:  # noqa: BLE001 — a notification failure must not break the run
                log.exception(
                    "run_workflow_streaming: failed to emit MCP notification for stage %r.",
                    report.get("stage"),
                )

    consumer = asyncio.create_task(_consume())
    try:
        result = await run_workflow_streamed(name, params, on_stage=_on_stage)
    finally:
        # Signal end-of-stream and DRAIN: guarantees every stage notification is sent
        # before the tool result is returned to the client (no lost final stage).
        loop.call_soon_threadsafe(queue.put_nowait, sentinel)
        await consumer
    return result


async def inspect_run(run_id: str, detail: bool = False) -> dict[str, Any]:
    """Return the per-step 'what ran' summary (status/timing/tool+LLM call counts) for a run."""
    from apecx_integration.composition.runtime.run_store import get_run_store

    if not isinstance(run_id, str) or not run_id.strip():
        return {
            "error": "inspect_run: 'run_id' must be a non-empty string (from a run_workflow result)."
        }
    record = get_run_store().get(run_id.strip())
    if record is None:
        known = [r.run_id for r in get_run_store().session_runs()]
        return {
            "error": f"inspect_run: unknown run_id {run_id!r}. "
            f"Known run_ids this session: {known or '(none)'}."
        }
    out: dict[str, Any] = {
        "run_id": record.run_id,
        "workflow_name": record.workflow_name,
        "status": record.status,
        "summary": record.run_summary.model_dump(mode="json"),
    }
    if detail and record.workflow_result is not None:
        out["result"] = record.workflow_result.model_dump(mode="json")
    return out


async def inspect_workflow(name: str, max_depth: int = 3) -> dict[str, Any]:
    """Return the recursive static structure (steps, links, nested workflows) of a workflow."""
    from apecx_integration.composition.inspection.workflow_inspector import (
        inspect_workflow as _inspect_yaml,
    )
    from apecx_integration.mcp_surface.workflow_registry import (
        _resolve_yaml_path,
        load_catalog,
    )

    if not isinstance(name, str) or not name.strip():
        return {"error": "inspect_workflow: 'name' must be a non-empty workflow name."}
    name = name.strip()
    catalog = load_catalog()
    entry = next((e for e in catalog.workflows if e.tool_name == name), None)
    if entry is None:
        available = sorted(e.tool_name for e in catalog.workflows)
        return {"error": f"inspect_workflow: unknown workflow {name!r}. Available: {available}."}
    if entry.source.kind != "yaml":
        return {
            "error": f"inspect_workflow: workflow {name!r} is a {entry.source.kind} callable; "
            "static YAML inspection is not applicable. Run it and use inspect_run instead."
        }
    try:
        resolved = _resolve_yaml_path(entry.source.path)
        inspection = _inspect_yaml(resolved, max_depth=max_depth)
    except Exception as exc:  # noqa: BLE001 — inspection failure is a loud error body
        return {
            "error": f"inspect_workflow: failed to inspect {name!r}: {type(exc).__name__}: {exc}"
        }
    return inspection.model_dump(mode="json")


async def apecx_context() -> dict[str, Any]:
    """Re-orient: the workflow runs made this session (run_id, name, status), oldest first.

    Scoped session state, not general memory — lets the external LLM recover its place after
    a context drop (which workflows it has already run, with which run_ids to inspect).
    """
    from apecx_integration.composition.runtime.run_store import get_run_store

    runs = [
        {"run_id": r.run_id, "workflow_name": r.workflow_name, "status": r.status}
        for r in get_run_store().session_runs()
    ]
    return {"runs": runs, "n_runs": len(runs)}


__all__ = [
    "apecx_context",
    "inspect_run",
    "inspect_workflow",
    "run_workflow",
    "run_workflow_streamed",
    "run_workflow_streaming",
]
