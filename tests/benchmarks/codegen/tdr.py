"""Test-Driven Recursive Refinement (TDR) codegen — G93 (2026-05-17).

A novel synthesis of test-driven development (TDD), Reflexion-style
failure memory, and execution-grounded reflection. NOT in any of the
three source papers (Yang RecursiveMAS / Wei Agentic Reasoning
Survey / Haidemariam) as a single named pattern, and NOT in the
project's 25 existing benchmarks.

Why TDR over the existing patterns
==================================

The project's ``review_revise`` family runs ONE round of LLM-judge
critique. The closest cousin in the literature is Reflexion (Shinn
et al.), which keeps verbal memory across rounds but still uses an
LLM as the critic. TDR replaces the LLM critic with **test
execution** — concrete, ground-truth, no hallucination — and keeps
the LLM only for failure analysis + code revision. This is honest
about a known weakness of LLM-as-judge: critic agreement with
ground truth degrades on the very problems where the LLM also fails
to generate correct code.

The loop
========

::

    0. (Initial)  LLM writes code given (prompt, tests, signature)
    1. (Execute)  Run code against tests in sandbox; capture stderr
    2. (Pass?)    If sandbox exit_code == 0  →  return code
    3. (Memory)   Append failure record (round, stderr_excerpt) to memory
    4. (Revise)   LLM writes new code given (prompt, tests, previous code,
                  this round's failure, full failure memory)
    5.            goto 1; cap at ``max_iterations`` rounds
    6.            return last attempt (whether it passes or not — the
                  runner makes its own pass/fail call)

Honest scope choices
====================

* **Per-round logic is in Python, not a nanobrain workflow YAML.**
  Nanobrain has no loop primitive (gap G18, LoopController, not yet
  shipped); a YAML-only TDR would require unrolling N rounds as N
  copies of the per-round step pair, which is ugly + fixes N at
  YAML-author time. The Python driver lets us choose N at call time
  + terminate early on success. The framework-capacity expansion
  proposal that would make TDR pure YAML lives in
  ``docs/tdr_pattern_2026-05-17.md``.
* **Failure parsing is intentionally crude.** We feed the LLM the
  raw sandbox stderr (truncated). A more sophisticated version
  would run each assertion separately to get per-test pass/fail,
  but that's 3-10× the sandbox cost. The crude version preserves
  the signal a human developer sees and is sufficient for v1.
* **No additional edge-case generation.** The TDR loop uses the
  benchmark's existing test_code; we do NOT have the LLM invent
  extra tests. Adding that is a v2 experiment (separating
  test-generation from code-generation costs).

Why nanobrain-framework-native (per workspace rule)
====================================================

The per-round LLM calls go through the same ``build_chat_llm``
factory the rest of the apecx codegens use; the sandbox is the
same one ``review_revise`` uses. The Python loop is the ONLY
non-nanobrain element, and it's the documented gap G18 (no loop
primitive yet). Once G18 lands, this module becomes a thin shim
around a YAML workflow.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from tests.benchmarks.codegen.direct import _FENCE_PATTERN, _default_llm_for
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.sandbox import run_in_subprocess
from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)

# Trim the stderr we feed the LLM so we don't blow up the prompt budget
# on a long traceback. The relevant signal (which assertion failed +
# the exception type) is always near the top.
_STDERR_MAX_CHARS = 2000


def _extract_code(text: str) -> str:
    """Extract Python code from a fenced ```python block, or return
    the whole text stripped if no fence (LLM occasionally drops the
    fence). The benchmark sandbox doesn't care about fences, but the
    runner wants just the code."""
    m = _FENCE_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _initial_codegen(
    llm: Any,
    problem: BenchmarkProblem,
) -> str:
    """Round-0 attempt — minimal prompt, same shape as direct codegen."""
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You write correct Python code. The user gives a problem; "
        "you respond with a single ```python fenced block containing "
        "the requested function (including all helper imports inside "
        "the block). No prose, no comments outside the block."
    )
    user_parts = [problem.prompt.strip()]
    if problem.test_code:
        # Show the LLM the actual test code so it sees the expected
        # API signature + concrete I/O examples. TDR is test-driven —
        # the tests are the spec.
        user_parts.append(
            f"Your function must satisfy these tests:\n```python\n{problem.test_code.strip()}\n```"
        )
    if problem.entry_point:
        user_parts.append(f"Define a function named ``{problem.entry_point}``.")

    response = llm.invoke(
        [
            SystemMessage(content=system),
            HumanMessage(content="\n\n".join(user_parts)),
        ]
    )
    return _extract_code(response.content)


def _revise_codegen(
    llm: Any,
    *,
    problem: BenchmarkProblem,
    previous_code: str,
    current_failure_stderr: str,
    failure_memory: list[str],
    round_num: int,
) -> str:
    """Round-N>0 attempt — uses failure memory to bias the rewrite.

    The prompt deliberately:
    * Shows the ACTUAL stderr (truncated), not an LLM-summarized
      version. The raw traceback is unambiguous; an LLM summary
      could lose the assertion line.
    * Includes the full memory of past failures (not just this
      round's). This is the Reflexion-style accumulating memory.
    * Asks for analysis BEFORE the code. We don't parse the analysis,
      but asking for it nudges the LLM toward genuine debugging
      vs. a syntactic re-roll.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    truncated = current_failure_stderr[:_STDERR_MAX_CHARS]
    if len(current_failure_stderr) > _STDERR_MAX_CHARS:
        truncated += "\n... [truncated]"

    memory_block = ""
    if failure_memory:
        memory_block = (
            "\n\nPast failure modes (accumulated across this debug session):\n"
            + "\n".join(f"  Round {i}: {fm}" for i, fm in enumerate(failure_memory))
        )

    system = (
        "You are debugging Python code. The user gives you: the original "
        "problem, the failing tests, the current code, and the test "
        "execution traceback. Your job is to fix the code.\n\n"
        "Respond with:\n"
        "1. A SHORT analysis (1-3 lines) of what went wrong.\n"
        "2. A single ```python fenced block with the corrected function. "
        "Include all imports inside the block. No prose after the block."
    )

    user_parts = [
        f"Problem:\n{problem.prompt.strip()}",
        f"Tests (must pass):\n```python\n{problem.test_code.strip()}\n```",
        f"Round {round_num + 1}: current code (failed):\n```python\n{previous_code}\n```",
        f"Test execution traceback:\n```\n{truncated}\n```{memory_block}",
        f"Define a function named ``{problem.entry_point}``." if problem.entry_point else "",
    ]
    user = "\n\n".join(p for p in user_parts if p)

    response = llm.invoke(
        [
            SystemMessage(content=system),
            HumanMessage(content=user),
        ]
    )
    return _extract_code(response.content)


def _failure_summary(stderr: str) -> str:
    """Extract a 1-line summary of the failure mode for the memory."""
    if not stderr:
        return "no stderr (exit nonzero with empty output)"
    # Look for the last AssertionError or last exception line — the
    # innermost failure is what we care about. Fallback to first 200
    # chars of the last non-empty line.
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return "empty stderr"
    # Find the last exception-shaped line.
    for line in reversed(lines):
        if re.match(r"^\w+(?:\.\w+)?(?:Error|Exception):\s", line):
            return line.strip()[:200]
        if "AssertionError" in line:
            return line.strip()[:200]
    return lines[-1][:200]


def make_tdr_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    max_iterations: int = 3,
    sandbox_timeout_seconds: float = 10.0,
    llm_factory: Callable[..., Any] | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build a TDR codegen function bound to a specific model.

    Args:
        model / base_url: LLM identity (resolves through ``model_roles``
            like every other codegen).
        max_iterations: Max recursive rounds AFTER the initial attempt.
            Default 3 → 1 initial + up to 3 revisions = 4 total LLM
            calls per problem. Raise for harder benchmarks; lower for
            cost-sensitive runs.
        sandbox_timeout_seconds: Per-execution wall budget. TDR will
            burn this once per round, so total time is roughly
            ``(iterations + 1) × (llm_latency + sandbox_timeout)``.
            10 s default matches the harness defaults.

    Returns a callable ``BenchmarkProblem -> str``. The returned code
    is whatever the LAST iteration produced — whether it passed or
    not. The runner makes the final pass/fail call by re-executing.
    """
    resolved_model, resolved_base = resolve_role(
        "drafter",
        kwarg_model=model,
        kwarg_base_url=base_url,
    )

    def _codegen(problem: BenchmarkProblem) -> str:
        if llm_factory is not None:
            llm = llm_factory(
                model=resolved_model,
                base_url=resolved_base,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            llm = _default_llm_for(resolved_model, resolved_base, temperature, max_tokens)

        # Round 0: initial attempt.
        code = _initial_codegen(llm, problem)

        failure_memory: list[str] = []

        for round_num in range(max_iterations):
            # Execute against the benchmark's tests.
            sandbox_result = run_in_subprocess(
                candidate_code=code,
                setup_code=problem.setup_code,
                test_code=problem.test_code,
                timeout_seconds=sandbox_timeout_seconds,
            )

            if sandbox_result.passed:
                log.info(
                    "TDR[%s]: passed at round %d (after %d revision%s)",
                    problem.problem_id,
                    round_num,
                    round_num,
                    "" if round_num == 1 else "s",
                )
                return code

            # Failed → analyze + revise.
            summary = _failure_summary(sandbox_result.stderr)
            failure_memory.append(summary)
            log.info(
                "TDR[%s]: round %d failure: %s",
                problem.problem_id,
                round_num,
                summary[:120],
            )

            code = _revise_codegen(
                llm,
                problem=problem,
                previous_code=code,
                current_failure_stderr=sandbox_result.stderr,
                failure_memory=failure_memory,
                round_num=round_num,
            )

        # All iterations exhausted; return last attempt for the runner
        # to make a final pass/fail call. The runner will likely fail
        # this case again — that's the honest outcome, not a TDR bug.
        log.info(
            "TDR[%s]: exhausted %d iterations without passing",
            problem.problem_id,
            max_iterations,
        )
        return code

    return _codegen
