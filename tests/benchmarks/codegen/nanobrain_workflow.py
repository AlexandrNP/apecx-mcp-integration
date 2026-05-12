"""CGU-P1-T6 — nanobrain-workflow-wrapped codegen adapter.

The benchmark runner takes a callable ``BenchmarkProblem -> str``.
The procedural codegens in this directory (``direct.py``,
``plan_then_code.py``) call Ollama directly via LangChain. This
adapter is the framework-native counterpart: a workflow YAML loads
via ``Workflow.from_config``, the runner packages each problem into
the workflow's input data unit, drives the cascade via
``wf.process`` + ``wf.wait_for_cascade``, and extracts the candidate
code string from the workflow's final-step output data unit.

Why this exists
---------------

``docs/composer_codegen_uplift_plan.md`` §0 rule 3 — every benchmark
codegen must be a nanobrain workflow, including the direct baseline.
Otherwise we cannot honestly attribute scaffold improvements to the
framework versus to the procedural prompt wiring.

Process-wide cache
------------------

``Workflow.from_config`` + ``Workflow.initialize`` is non-cheap
(~80ms). We cache the loaded+initialized workflow per (yaml_path,
event_loop) tuple — the event-loop key is needed because async data
units bind to the current loop when initialize() runs, so reusing a
workflow across loops crashes. The benchmark runner uses a single
loop per process so the cache hits in practice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)


def make_nanobrain_workflow_codegen(
    workflow_yaml_path: str | Path,
    *,
    first_step_input_du_name: str = "drafter_input",
    code_source_step_name: str = "drafter",
    code_source_du_name: str = "drafter_output",
    cascade_timeout_seconds: float = 120.0,
) -> Callable[[BenchmarkProblem], str]:
    """Build a benchmark codegen that runs through a nanobrain workflow.

    ``first_step_input_du_name`` (default ``drafter_input``) is the
    FIRST STEP's input DataUnit name. ``Workflow.process(input_data)``
    writes the dict's values to the matching step input DUs by name —
    NOT to workflow-level input DUs. See rag_e2e's
    ``test_workflow_runtime_executes_via_trigger_cascade`` for the
    canonical drive pattern. Writing to a workflow-level DU name
    (e.g., ``workflow_input``) is a silent no-op; the cascade fires
    but the first step's trigger never sees the payload.

    ``code_source_step_name``/``code_source_du_name`` (defaults
    ``drafter``/``drafter_output``) name the step + its output DU
    from which the adapter reads the final ``code_source`` string.
    We read from the step's DU directly rather than from a workflow-
    level output DU because (a) the step's DU is the canonical
    source-of-truth that the framework writes immediately on
    ``process()`` return, and (b) workflow-level output DUs require
    one additional link hop that adds latency without changing the
    benchmark outcome.

    The returned callable is synchronous (matches the runner's
    ``CodegenFn`` shape). It runs a fresh event loop per call to
    avoid asyncio cross-call state.
    """
    workflow_yaml = Path(workflow_yaml_path).resolve()
    if not workflow_yaml.is_file():
        raise ValueError(f"workflow_yaml_path does not exist: {workflow_yaml}")

    # The cached workflow + a re-init flag. Re-init is needed if the
    # event loop changes (e.g., pytest creates a new loop per test).
    state: dict[str, Any] = {"loop": None, "wf": None}

    def _codegen(problem: BenchmarkProblem) -> str:
        return asyncio.run(_run_async(problem))

    async def _run_async(problem: BenchmarkProblem) -> str:
        from nanobrain.core.workflow import Workflow  # noqa: PLC0415

        current_loop = asyncio.get_running_loop()
        if state["loop"] is not current_loop or state["wf"] is None:
            log.debug("loading workflow %s (loop changed or first call)", workflow_yaml)
            wf = Workflow.from_config(str(workflow_yaml))
            await wf.initialize()
            state["wf"] = wf
            state["loop"] = current_loop
        wf = state["wf"]

        input_dict = _package_problem(problem)
        init_result = await wf.process({first_step_input_du_name: input_dict})
        if not isinstance(init_result, dict) or init_result.get("status") not in (
            "data_flow_initiated",
            "completed",
        ):
            raise RuntimeError(
                f"workflow {workflow_yaml.name}: process() returned unexpected "
                f"envelope {init_result!r}"
            )

        drained = await wf.wait_for_cascade(timeout=cascade_timeout_seconds, settle_ms=100)
        if not drained:
            raise RuntimeError(
                f"workflow {workflow_yaml.name}: cascade did not drain "
                f"within {cascade_timeout_seconds}s — trigger hang or "
                f"LLM call exceeded budget"
            )

        children = (
            getattr(wf, "child_steps", None)
            or getattr(wf, "_child_steps", None)
            or getattr(wf, "steps", None)
            or {}
        )
        step = children.get(code_source_step_name)
        if step is None:
            raise RuntimeError(
                f"workflow {workflow_yaml.name}: step "
                f"{code_source_step_name!r} not found; available={list(children)}"
            )

        out_dus = step.step_output_data_units
        out_du = out_dus.get(code_source_du_name)
        if out_du is None:
            raise RuntimeError(
                f"workflow {workflow_yaml.name}: step output DU "
                f"{code_source_du_name!r} not found; "
                f"available={list(out_dus.keys())}"
            )

        value = await out_du.get()
        # Step output DUs hold the dict the step's process() returned.
        # We accept either:
        #   * dict {code_source: str}      — canonical drafter shape
        #   * dict envelope {drafter_output: {code_source: str}}
        #     — what the trigger system wraps when re-serializing.
        #   * str                          — drafter returned a bare string
        if isinstance(value, dict):
            if "code_source" in value:
                return _ensure_str(value["code_source"])
            if code_source_du_name in value and isinstance(value[code_source_du_name], dict):
                inner = value[code_source_du_name]
                return _ensure_str(inner.get("code_source", ""))
        return _ensure_str(value)

    return _codegen


def _package_problem(problem: BenchmarkProblem) -> dict[str, Any]:
    """Turn a BenchmarkProblem into the input dict the drafter expects.

    Mirrors the procedural direct codegen's prompt-shape decisions so
    the workflow-wrap reproduces the procedural baseline within
    benchmark noise: prompt is the raw problem.prompt; entry_point
    and the first line of test_code are passed as optional hints.
    """
    payload: dict[str, Any] = {"code_spec": problem.prompt.strip()}
    if problem.entry_point:
        payload["entry_point"] = problem.entry_point
    if problem.test_code:
        first_line = problem.test_code.splitlines()[0]
        payload["test_hint"] = first_line
    return payload


def _ensure_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


__all__ = ["make_nanobrain_workflow_codegen"]
