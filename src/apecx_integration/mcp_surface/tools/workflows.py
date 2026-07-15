"""MCP tools for the scientist-facing workflow lifecycle.

Each tool is a thin wrapper over ``ControlPlaneClient`` that marshals
MCP-friendly input types (strs) into the Control Plane's request
envelopes and returns plain dicts / strings MCP can serialize.

Names follow AP §3 — these are what Claude Desktop surfaces as tools.
"""

from __future__ import annotations

from apecx_integration.control_plane.schemas.api import (
    ExecuteWorkflowRequest,
    ShowYamlDiffRequest,
    StartWorkflowRequest,
)
from apecx_integration.control_plane.schemas.enums import ExecutorKind
from apecx_integration.mcp_surface.tools._shared import (
    get_client,
    parse_run_id,
)

_VALID_EXECUTORS = {e.value for e in ExecutorKind}


async def compose_workflow(
    description: str = "",
    user_id: str = "anonymous",
    preferred_executor: str = "local",
    run_id: str = "",
    execute: bool = False,
) -> dict:
    """Compose (and optionally run) a NEW workflow from a natural-language description.

    Use this ONLY when ``list_workflows`` finds no existing catalog workflow that
    fits — for anything already in the catalog use ``run_workflow(name, params)``.
    This is the SINGLE composer primitive (it replaces the former
    ``start_workflow`` / ``show_diff`` / ``execute_workflow`` split, which competed
    with ``run_workflow`` and split one decision across three calls).

    Return-of-control flow — the T06 differential review is folded in:

      1. **COMPOSE** — call with ``description`` (leave ``execute``
         False). Returns ``run_id``, ``status``, ``generated_workflow_artifact_id``,
         and ``review`` (the T06 diff: ``yaml_text``, per-step ``categorization``,
         ``summary_sentence``, ``novel_python_by_step``) so the frontier LLM /
         scientist reviews BEFORE running. Control returns to the caller here.
      2. **EXECUTE** — after review, re-call with the returned ``run_id`` +
         ``execute=True``. Returns the terminal ``execution`` result.

    Pass ``execute=True`` in step 1 to compose-and-run in a single call.
    """
    client = get_client()
    out: dict = {}

    if not run_id:
        if not description:
            raise ValueError(
                "compose_workflow: provide `description` to compose a new workflow, "
                "OR `run_id` (+ execute=True) to run a previously-composed one."
            )
        if preferred_executor not in _VALID_EXECUTORS:
            # Echo the offending value back (vs the opaque Pydantic enum-coercion error).
            raise ValueError(
                f"preferred_executor={preferred_executor!r} is not a valid executor; "
                f"expected one of {sorted(_VALID_EXECUTORS)}."
            )
        # LLM-availability PREFLIGHT (#4, 2026-07-01). Composition ALWAYS needs the apecx LLM: the
        # composer drives its OWN LLM factory (this is NOT the desktop final_synthesis inversion),
        # so with Ollama off (the desktop default) start_workflow would die at the first compose
        # call with a raw ConnectError (composer max_retries: 0) — an opaque traceback to the caller.
        # Refuse LOUDLY + actionably instead. Reuse the single-source llm_policy.resolve_llm (real
        # probe) per the "canonical surfaces — reuse, don't rebuild" rule; do NOT add a parallel probe.
        from apecx_integration.composition.runtime.execution_locus import (  # noqa: PLC0415
            get_active_locus,
        )
        from apecx_integration.mcp_surface.llm_policy import resolve_llm  # noqa: PLC0415

        _llm = resolve_llm(get_active_locus())
        if not _llm.available:
            return {
                "error": "compose_workflow needs an apecx LLM backend and none is reachable",
                "detail": _llm.detail,
                "hint": (
                    "start a local Ollama server (localhost:11434) or set APECX_LLM_BASE_URL to a "
                    "reachable OpenAI-compatible endpoint, then retry compose_workflow"
                ),
            }
        started = await client.start_workflow(
            StartWorkflowRequest(
                description=description,
                user_id=user_id,
                preferred_executor=ExecutorKind(preferred_executor),
            )
        )
        started_d = started.model_dump(mode="json")
        out["run_id"] = started_d["run"]["id"]
        out["status"] = started_d["run"].get("status")
        out["generated_workflow_artifact_id"] = started_d.get("generated_workflow_artifact_id")
        if started_d.get("pause_reason"):
            out["pause_reason"] = started_d["pause_reason"]
        # Fold in the T06 differential review so the caller decides before running.
        diff = await client.show_yaml_diff(ShowYamlDiffRequest(run_id=parse_run_id(out["run_id"])))
        out["review"] = diff.model_dump(mode="json")
        target = out["run_id"]
    else:
        out["run_id"] = run_id
        target = run_id

    if execute:
        result = await client.execute_workflow(ExecuteWorkflowRequest(run_id=parse_run_id(target)))
        out["execution"] = result.model_dump(mode="json")

    return out


__all__ = ["compose_workflow"]
