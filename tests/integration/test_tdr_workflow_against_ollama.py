"""G99 TDR-as-YAML — live-Ollama end-to-end test.

Three real round-trip measurements:

  1. Single iteration that should PASS on first try (simple add(a, b)
     with passing tests). Verifies the short-circuit path:
     workflow_input → tdr_iter → ConditionalLink(exec_succeeded=true)
     → final_code. iteration should be 1 in the result.

  2. Cycle that should iterate at least once (a problem where TDR's
     first attempt typically fails and revision fixes it; we don't
     assert the LLM converges — only that the loop fires more than
     once, proving the back-edge through LoopController works
     end-to-end). iteration should be ≥ 2.

  3. Loop-exhaustion path (a deliberately unsatisfiable test that no
     LLM can satisfy). Verifies the escalation path:
     loop_gate emits loop_exhausted=true →
     ConditionalLink(loop_exhausted=true) → final_code. Result
     carries exec_succeeded=false.

Auto-skips when Ollama is unreachable OR when APECX_CODE_EXEC is not
set (IsolatedPyExecStep refuses to execute without the env opt-in).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest
from nanobrain.core.workflow import Workflow

pytestmark = pytest.mark.integration


_WORKFLOW_YAML = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "tdr_loop"
    / "tdr_refine_workflow.yml"
)


def _llm_reachable() -> bool:
    base = os.environ.get("APECX_LLM_BASE_URL") or "http://localhost:11434/v1"
    probe = base[:-3] + "/api/tags" if base.endswith("/v1") else base.rstrip("/") + "/api/tags"
    try:
        return httpx.get(probe, timeout=2.0).status_code == 200
    except Exception:
        return False


SKIP_LLM = "LLM not reachable — set APECX_LLM_BASE_URL"
SKIP_EXEC = "Set APECX_CODE_EXEC=1 to enable real subprocess execution"


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
@pytest.mark.skipif(os.environ.get("APECX_CODE_EXEC") != "1", reason=SKIP_EXEC)
def test_tdr_workflow_short_circuit_on_first_pass():
    """A simple problem that should pass on first iteration.
    Verifies the short-circuit ConditionalLink fires when
    exec_succeeded=true on iteration 1."""
    workflow = Workflow.from_config(str(_WORKFLOW_YAML))

    initial = {
        "code_spec": (
            "Write a function add(a: int, b: int) -> int that returns the sum of a and b."
        ),
        "function_name": "add",
        "function_signature": "def add(a: int, b: int) -> int",
        "test_code": ("assert add(2, 3) == 5\nassert add(0, 0) == 0\nassert add(-1, 1) == 0\n"),
        "entrypoint": "add",
    }

    start = time.monotonic()
    result = asyncio.run(_run(workflow, initial, timeout=120))
    elapsed = time.monotonic() - start

    assert elapsed < 120, f"Workflow took {elapsed:.1f}s; expected ≤ 120s"
    assert result is not None, "Workflow produced no output"
    assert result["exec_succeeded"] is True, (
        f"Even the trivial add() problem failed at TDR. stderr: {result.get('stderr')!r}"
    )
    # Short-circuit means iteration is 1 (no loop_gate involvement).
    assert result["iteration"] == 1, (
        f"Expected first-iter pass (iteration=1), got "
        f"{result['iteration']}. May indicate the short-circuit "
        f"ConditionalLink fired the wrong predicate."
    )
    assert "code_source" in result and "def add" in result["code_source"]


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
@pytest.mark.skipif(os.environ.get("APECX_CODE_EXEC") != "1", reason=SKIP_EXEC)
def test_tdr_workflow_back_edge_fires_on_loop_exhaustion():
    """Deliberately unsatisfiable test forces the loop to exhaust.
    Verifies the LoopController's loop_exhausted path fires
    end-to-end (loop_gate.output → ConditionalLink → final_code)."""
    workflow = Workflow.from_config(str(_WORKFLOW_YAML))

    initial = {
        "code_spec": ("Write a function ones() that returns the integer 1. No arguments."),
        "function_name": "ones",
        "function_signature": "def ones() -> int",
        # IMPOSSIBLE: asserts the function returns BOTH 1 and 2. No
        # LLM can satisfy this; loop will exhaust.
        "test_code": "assert ones() == 1\nassert ones() == 2\n",
        "entrypoint": "ones",
    }

    # Wall-clock budget is set on the workflow itself (600s in the
    # workflow YAML's ``execution.timeout``). We deliberately don't
    # assert a Python-side time budget here — a 3-iteration loop with
    # ~60-150s LLM calls each can legitimately take 7+ minutes on
    # mistral-nemo and we don't want that to be a flake source.
    result = asyncio.run(_run(workflow, initial, timeout=650))
    assert result is not None
    # Loop should exhaust → exec_succeeded stays false on final attempt.
    assert result["exec_succeeded"] is False, (
        "Workflow somehow satisfied an unsatisfiable test (returns "
        "both 1 and 2). Either the test_code wasn't actually run or "
        "the workflow short-circuited on a false positive."
    )
    # The loop_gate is configured with max_iterations=3, so a fully
    # exhausted loop reaches iteration=3 (3 attempts, all failed).
    assert result["iteration"] == 3, (
        f"Expected loop exhaustion at iteration=3, got "
        f"{result['iteration']}. Either the back-edge didn't fire "
        f"(iteration < 3) or the LoopController over-iterated."
    )


async def _run(workflow: Workflow, initial: dict, *, timeout: float):
    """Drive the workflow.

    Per Workflow.run / Workflow.process signature: in data-driven mode
    the runner populates the **first step's** input data units, NOT
    the workflow-level input_data_units. So the payload key here is
    ``tdr_iter_input`` (the first step's input unit name), not
    ``workflow_input`` (which is a workflow-level declaration that the
    runtime cascade does not write to). See the existing rag_e2e
    workflow integration test for the same pattern.
    """
    outputs = await workflow.run(
        {"tdr_iter_input": initial},
        timeout=timeout,
        settle_ms=200,
    )
    assert outputs is not None, "workflow.run returned None"
    assert "final_code" in outputs, (
        f"final_code not in workflow outputs; got keys: {list(outputs.keys())}"
    )
    return outputs["final_code"]
