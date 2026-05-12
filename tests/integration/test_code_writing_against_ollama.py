"""CW-11 — live-Ollama integration tests for the code-writing stack.

Three real round-trip measurements:

  1. ``CodeWriteStep`` standalone — generates Python for a simple
     spec; AST gate confirms the output parses; function-name gate
     confirms the requested name is defined.

  2. ``CodeReviewStep`` standalone — reviews known-good and
     known-bad code samples; verifies the structured JSON verdict
     parses + has the expected shape.

  3. ``code_reflection_workflow.yml`` end-to-end — the canonical
     write → review cascade as a real Workflow run. This exercises
     the SubworkflowStep-equivalent invocation path: the outer
     caller treats it as a single function-call interface.

  4. ``code_authoring_with_reflection_and_verification.yml`` —
     full outer workflow with the SubworkflowStep wrapping. Requires
     APECX_CODE_EXEC=1 (verification subprocess gate).

What these tests intentionally DO NOT assert:
  - Specific code output (small-LLM variance).
  - Specific approved=True/False outcome (variance again).
  - Specific elapsed time (varies by host hardware).

What they DO assert:
  - Each layer terminates without raising.
  - Each layer's output dict has the documented keys.
  - The AST gate's contract holds (output is parseable Python).
  - The reviewer's contract holds (structured JSON verdict).
  - End-to-end timing is bounded at 240s (catches accidental hangs).

Auto-skips when Ollama is unreachable. Set APECX_LLM_BASE_URL +
APECX_LLM_MODEL to run.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest

from apecx_integration.composition.steps.code_review_step import (
    CodeReviewStep,
)
from apecx_integration.composition.steps.code_write_step import (
    CodeWriteStep,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_WRITING_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing"
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


_FIZZBUZZ_SPEC = (
    "Write a function fizzbuzz(n: int) -> str. For multiples of 3 return "
    "'Fizz'; for multiples of 5 return 'Buzz'; for multiples of both "
    "return 'FizzBuzz'; otherwise return str(n). For n < 1 raise "
    "ValueError."
)


# ---------------------------------------------------------------------------
# 1. CodeWriteStep against real LLM
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_code_write_generates_parseable_python_for_fizzbuzz():
    wrapper = CODE_WRITING_DIR / "steps" / "code_write.yml"
    step = CodeWriteStep.from_config(str(wrapper))

    start = time.monotonic()
    result = asyncio.run(
        step.process(
            {
                "code_spec": _FIZZBUZZ_SPEC,
                "function_name": "fizzbuzz",
                "function_signature": "def fizzbuzz(n: int) -> str",
            }
        )
    )
    elapsed = time.monotonic() - start

    assert elapsed < 180.0, f"code_write took too long: {elapsed:.1f}s"

    # The step's AST gate already enforced parseability — if we got
    # here without raising, the source IS valid Python with the
    # requested function. Pin the passthrough fields too.
    assert result["code_source"].strip(), "empty code_source"
    assert result["function_name_verified"] == "fizzbuzz"
    assert result["code_spec"] == _FIZZBUZZ_SPEC
    print(f"\n[code_write] elapsed={elapsed:.2f}s; source={len(result['code_source'])} chars")


# ---------------------------------------------------------------------------
# 2. CodeReviewStep against real LLM
# ---------------------------------------------------------------------------


_GOOD_CODE = (
    "def fizzbuzz(n: int) -> str:\n"
    "    if n < 1:\n"
    "        raise ValueError(f'expected n >= 1, got {n}')\n"
    "    if n % 15 == 0:\n"
    "        return 'FizzBuzz'\n"
    "    if n % 3 == 0:\n"
    "        return 'Fizz'\n"
    "    if n % 5 == 0:\n"
    "        return 'Buzz'\n"
    "    return str(n)\n"
)

_WRONG_CODE = (
    "def fizzbuzz(n: int) -> str:\n"
    "    # WRONG: returns 'Fizz' for multiples of 5 (spec says Buzz)\n"
    "    if n % 5 == 0:\n"
    "        return 'Fizz'\n"
    "    return str(n)\n"
)


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_code_review_emits_structured_verdict_on_real_llm():
    wrapper = CODE_WRITING_DIR / "steps" / "code_review.yml"
    step = CodeReviewStep.from_config(str(wrapper))

    start = time.monotonic()
    verdict = asyncio.run(
        step.process(
            {
                "code_source": _GOOD_CODE,
                "code_spec": _FIZZBUZZ_SPEC,
                "function_name": "fizzbuzz",
                "function_signature": "def fizzbuzz(n: int) -> str",
            }
        )
    )
    elapsed = time.monotonic() - start

    assert elapsed < 90.0, f"review took too long: {elapsed:.1f}s"

    # Pin the structured-shape contract — what callers downstream rely on.
    assert isinstance(verdict["approved"], bool)
    assert isinstance(verdict["reasoning"], str)
    assert isinstance(verdict["concerns"], list)
    assert isinstance(verdict["suggestions"], list)
    assert isinstance(verdict["raw_response"], str)
    print(
        f"\n[code_review good] elapsed={elapsed:.2f}s; "
        f"approved={verdict['approved']}; "
        f"concerns={len(verdict['concerns'])}; "
        f"suggestions={len(verdict['suggestions'])}"
    )


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_code_review_flags_wrong_code_with_concerns():
    """The reviewer should reject the wrong code. We don't pin the
    exact verdict (small-model variance), but if approved=False it
    MUST come with concerns; if approved=True, that's a false
    positive logged for visibility but not a test failure (per the
    workspace 'measurement not pinning' philosophy for LLM tests)."""
    wrapper = CODE_WRITING_DIR / "steps" / "code_review.yml"
    step = CodeReviewStep.from_config(str(wrapper))

    verdict = asyncio.run(
        step.process(
            {
                "code_source": _WRONG_CODE,
                "code_spec": _FIZZBUZZ_SPEC,
                "function_name": "fizzbuzz",
            }
        )
    )

    assert isinstance(verdict["approved"], bool)
    if not verdict["approved"]:
        # Grounded-rejection gate must have fired: rejection without
        # concerns would have raised in the step itself.
        assert len(verdict["concerns"]) > 0
        print(f"\n[code_review wrong] approved=False; concerns={verdict['concerns'][:2]}")
    else:
        print(
            "\n[code_review wrong] approved=True (false positive — "
            "small-model variance; not a test failure)"
        )


# ---------------------------------------------------------------------------
# 3. End-to-end code_reflection_workflow.yml
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_code_reflection_workflow_end_to_end():
    """Run the inner reflection workflow via the established
    apecx pattern: deposit into the first step's input data unit
    directly, drain the cascade, read from the last step's output
    data unit.

    Pre-2026-05-12 this test was xfail-strict: the framework's
    ``Workflow.process`` did not auto-initialize, so triggers stayed
    unbound and the cascade silently no-op'd. Fixed at nanobrain side
    in ``Workflow.process`` (auto-init when a first step is present);
    this test now exercises the full write → review cascade end-to-end.
    """
    from nanobrain.core.workflow import Workflow

    workflow_path = CODE_WRITING_DIR / "code_reflection_workflow.yml"
    wf = Workflow.from_config(str(workflow_path))

    short_spec = "Write a function add(a: int, b: int) -> int that returns a + b."

    async def _drive_cascade():
        init = await wf.process(
            {
                "code_write_input": {
                    "code_spec": short_spec,
                    "function_name": "add",
                }
            }
        )
        assert init.get("status") in (
            "data_flow_initiated",
            "completed",
        ), f"unexpected init status: {init!r}"
        # Drain the cascade — give it 240s to cover two LLM round-trips.
        drained = await wf.wait_for_cascade(timeout=240.0, settle_ms=200)
        assert drained, "cascade did not drain within 240s"
        # Read the review step's output data unit (the LAST step's output).
        review_step = wf.child_steps["code_review"]
        out_du = review_step.step_output_data_units["code_review_output"]
        return await out_du.get()

    start = time.monotonic()
    verdict = asyncio.run(_drive_cascade())
    elapsed = time.monotonic() - start

    print(
        f"\n[reflection_workflow] elapsed={elapsed:.2f}s; "
        f"verdict shape={type(verdict).__name__}; "
        f"verdict keys={sorted(verdict.keys()) if isinstance(verdict, dict) else verdict!r}"
    )
    # The reviewer's structured verdict must be present.
    assert isinstance(verdict, dict), f"expected dict verdict, got {type(verdict).__name__}"
    for key in ("approved", "reasoning", "concerns", "suggestions"):
        assert key in verdict, f"reviewer verdict missing {key!r}: {sorted(verdict.keys())}"


# ---------------------------------------------------------------------------
# 4. Outer demo workflow (requires APECX_CODE_EXEC=1)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Nested-SubworkflowStep cascade has a multi-layered framework "
        "gap that resists single-fix mitigation. Sub-problems "
        "identified + partially mitigated 2026-05-12: (a) singleton "
        "AsyncTriggerExecutor causes wait_for_cascade re-entrance "
        "deadlock (mitigated by polling in SubworkflowStep + here in "
        "the test); (b) input routing double-wraps the framework's "
        "step-input-DU envelope (fixed in nanobrain 3c7a725); (c) "
        "output collection used last-step only, missing workflow-"
        "level outputs (fixed in same commit). After all three fixes "
        "the outer cascade STILL hangs — additional unknown layer "
        "below. Documented in friction-log #29. Single-level "
        "SubworkflowStep usage works (inner reflection cascade runs "
        "end-to-end in 30s); nested two-level usage is the remaining "
        "blocker. xfail-strict so a future fix surfaces."
    ),
)
@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
@pytest.mark.skipif(os.environ.get("APECX_CODE_EXEC") != "1", reason=SKIP_EXEC)
def test_outer_workflow_with_reflection_and_verification():
    """Full workflow-of-workflows: reflection sub-workflow generates +
    reviews code; verification sub-workflow runs it in a subprocess.
    Top-level should complete with a populated exec_result whose
    elapsed_seconds is non-trivial (catches the silent-cascade-
    failure shape that would otherwise let this 'pass' in <0.1s)."""
    from nanobrain.core.workflow import Workflow

    workflow_path = CODE_WRITING_DIR / "code_authoring_with_reflection_and_verification.yml"
    wf = Workflow.from_config(str(workflow_path))

    async def _drive_cascade():
        await wf.process(
            {
                "code_reflection_input": {
                    "code_spec": "Write add(a: int, b: int) -> int returning a + b.",
                    "function_name": "add",
                }
            }
        )
        # CANNOT call wf.wait_for_cascade here: same re-entrance
        # deadlock as the SubworkflowStep itself faced — the
        # AsyncTriggerExecutor is a process-wide singleton, and the
        # SubworkflowStep tasks driving the inner cascades are in
        # its task list. wait_for_cascade would wait for those tasks
        # to drain while the tasks themselves are polling output
        # data units. Workaround: poll the final output DU here
        # (matches the SubworkflowStep internal pattern; see
        # friction-log #29).
        verification_step = wf.child_steps["code_verification"]
        out_du = verification_step.step_output_data_units["code_verification_output"]
        deadline = asyncio.get_event_loop().time() + 240.0
        while True:
            val_check = await out_du.get()
            if val_check is not None:
                break
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(
                    "outer cascade did not populate code_verification_output within 240s"
                )
            await asyncio.sleep(0.25)
        return await out_du.get()

    start = time.monotonic()
    final = asyncio.run(_drive_cascade())
    elapsed = time.monotonic() - start

    print(f"\n[outer_workflow] elapsed={elapsed:.2f}s; final shape={type(final).__name__}")
    # Silent-cascade pin: anything under 5s means the cascade did not
    # actually invoke the LLM. Real cascade is ~30-60s.
    assert elapsed > 5.0, (
        f"outer cascade completed too fast ({elapsed:.2f}s) — the "
        f"LLM round-trips did not happen. Check trigger wiring."
    )
    assert isinstance(final, dict), f"expected exec_result dict, got {type(final).__name__}"
    exec_result = final.get("exec_result", final)
    assert "stdout" in exec_result and "returncode" in exec_result
