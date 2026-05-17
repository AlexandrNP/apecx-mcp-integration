"""TDR-as-YAML codegen — drives the framework-native TDR workflow.

Variant of ``tdr.py`` (the Python-driven TDR refine loop) that uses
the nanobrain workflow at
``src/apecx_integration/composition/workflows/tdr_loop/tdr_refine_workflow.yml``
instead of an imperative Python loop. The two produce equivalent
results — the YAML path proves the framework's iteration primitives
(LoopController + ConditionalLink + cycle validator) are sufficient
to express iterative refinement declaratively.

Cost characteristics are identical to ``tdr.py`` (1-3 iterations,
each = 1 LLM call + 1 subprocess). The wall time MAY be slightly
higher due to async trigger overhead per iteration; the wall
difference is the cost of declarative orchestration.

Use this codegen via:

    PYTHONPATH=src .venv/bin/python -m tests.benchmarks.cli \\
        mbpp --codegen tdr_yaml --limit 5 --output /tmp/parity.json

Environment requirements (same as ``tdr``):
  - APECX_CODE_EXEC=1 — IsolatedPyExecStep gate
  - APECX_LLM_BASE_URL / APECX_LLM_MODEL — Ollama or compatible endpoint
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from nanobrain.core.workflow import Workflow

from tests.benchmarks.codegen.direct import _FENCE_PATTERN
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)

_WORKFLOW_YAML = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "tdr_loop"
    / "tdr_refine_workflow.yml"
)


def _extract_code(text: str) -> str:
    """Pull a Python source block out of either a fenced response or
    raw text. Matches the behavior of the Python TDR codegen so a
    parity test stays meaningful."""
    if not isinstance(text, str):
        return ""
    m = _FENCE_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def make_tdr_yaml_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build a codegen function bound to a model + base_url.

    Returns ``BenchmarkProblem -> str`` (the final code_source string
    the workflow produced, whether it passed the tests or not — the
    benchmark runner re-executes to make the final pass/fail call).

    Honest scope choices:
      * Workflow YAML defines the iteration cap (max_iterations=3 in
        steps/loop_gate.yml). This codegen does not override per-call.
      * model / base_url override the wrapper's ``APECX_LLM_*`` env vars
        only via the role-resolution layer; no direct YAML mutation.
      * The wrapper YAML's CodeWriteStep config (temperature=0,
        max_tokens=1024) applies to every iteration's LLM call.
    """
    # Resolve role (drafter = the writer role) for parity with the
    # Python TDR codegen. Env-var lookups happen here, once.
    resolved_model, resolved_base = resolve_role(
        "drafter",
        kwarg_model=model,
        kwarg_base_url=base_url,
    )
    # The role values are surfaced via APECX_LLM_* env vars that the
    # underlying CodeWriteStep + build_chat_llm read; setting them
    # here mirrors the Python TDR codegen's resolve+pass-through.
    import os

    if resolved_model and not os.environ.get("APECX_LLM_MODEL"):
        os.environ["APECX_LLM_MODEL"] = resolved_model
    if resolved_base and not os.environ.get("APECX_LLM_BASE_URL"):
        os.environ["APECX_LLM_BASE_URL"] = resolved_base

    def _codegen(problem: BenchmarkProblem) -> str:
        # Workflow is constructed PER PROBLEM. The LoopController's
        # iteration counter is bound to the instance, so re-using a
        # workflow across problems would leak state. Build fresh.
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
                {"tdr_iter_input": initial_envelope},
                timeout=420.0,
                settle_ms=200,
            )
            return outputs.get("final_code")

        result = asyncio.run(_drive())
        if result is None:
            log.warning(
                "TDR-YAML codegen produced no final_code for problem %s — "
                "returning empty string so the benchmark runner records "
                "the failure honestly rather than hiding it.",
                problem.problem_id,
            )
            return ""

        code = result.get("code_source", "")
        log.info(
            "TDR-YAML codegen %s: iteration=%s exec_succeeded=%s code_chars=%d",
            problem.problem_id,
            result.get("iteration"),
            result.get("exec_succeeded"),
            len(code),
        )
        return _extract_code(code)

    return _codegen
