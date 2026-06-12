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

import logging
from typing import Any

log = logging.getLogger(__name__)

_SUCCESS_STATUSES = {"completed", "completed_no_await"}


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
        stamped = outcome.workflow_result.model_copy(update={"run_id": record.run_id})
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


__all__ = ["apecx_context", "inspect_run", "inspect_workflow", "run_workflow"]
