"""Best-of-N as a framework-native YAML workflow (G104 codegen).

Variant of ``best_of_n.py`` (Python-driven loop) that uses the
nanobrain workflow at
``src/apecx_integration/composition/workflows/best_of_n_loop/best_of_n_workflow.yml``.

Functional equivalent to ``best_of_n`` (independent samples + first
that passes test_code wins) but driven by the framework cycle
primitives (LoopController + ConditionalLink). The shared
TdrIterationStep class (in best_of_n mode) does the per-sample
LLM call + exec.

Environment requirements (same as ``tdr_yaml``):
  - APECX_CODE_EXEC=1 — IsolatedPyExecStep gate
  - APECX_LLM_BASE_URL / APECX_LLM_MODEL — Ollama or compatible endpoint
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

from nanobrain.core.workflow import Workflow

from tests.benchmarks.codegen.direct import _FENCE_PATTERN
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)

# parents[0]=codegen, [1]=benchmarks, [2]=tests, [3]=repo-root.
_WORKFLOW_YAML = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "best_of_n_loop"
    / "best_of_n_workflow.yml"
)


def _extract_code(text: str) -> str:
    if not isinstance(text, str):
        return ""
    m = _FENCE_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def make_best_of_n_yaml_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build a best-of-N YAML codegen function bound to a model + base_url.

    Returns ``BenchmarkProblem -> str``. Same contract as
    ``make_best_of_n_codegen`` (the Python variant) — returns the
    final code_source the workflow produced.

    The workflow YAML defines sample count (max_iterations=3 in
    steps/loop_gate.yml) and sample temperature (0.4 in
    steps/code_writer.yml). Operators tune via those files.
    """
    resolved_model, resolved_base = resolve_role(
        "drafter",
        kwarg_model=model,
        kwarg_base_url=base_url,
    )
    if resolved_model and not os.environ.get("APECX_LLM_MODEL"):
        os.environ["APECX_LLM_MODEL"] = resolved_model
    if resolved_base and not os.environ.get("APECX_LLM_BASE_URL"):
        os.environ["APECX_LLM_BASE_URL"] = resolved_base

    def _codegen(problem: BenchmarkProblem) -> str:
        # Fresh workflow per problem — LoopController instance state
        # would leak across problems otherwise.
        workflow = Workflow.from_config(str(_WORKFLOW_YAML))

        initial_envelope = {
            "code_spec": problem.prompt,
            "function_name": problem.entry_point or None,
            "function_signature": None,
            "test_code": problem.test_code,
            "entrypoint": problem.entry_point or None,
        }

        async def _drive() -> dict | None:
            outputs = await workflow.run(
                {"best_of_n_iter_input": initial_envelope},
                timeout=300.0,
                settle_ms=200,
            )
            return outputs.get("final_code")

        result = asyncio.run(_drive())
        if result is None:
            log.warning(
                "best_of_n_yaml produced no final_code for %s — returning "
                "empty so the runner records the failure honestly.",
                problem.problem_id,
            )
            return ""

        code = result.get("code_source", "")
        log.info(
            "best_of_n_yaml %s: iteration=%s exec_succeeded=%s code_chars=%d",
            problem.problem_id,
            result.get("iteration"),
            result.get("exec_succeeded"),
            len(code),
        )
        return _extract_code(code)

    return _codegen
