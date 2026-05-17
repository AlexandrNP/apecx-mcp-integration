"""Hybrid TDR + Best-of-N codegen — G107 (2026-05-17).

Combines the two mechanisms that the G96 + G101 + G102+G103
empirical analysis showed are complementary:

* **Best-of-N**: independent samples at non-zero temperature; first
  that passes test_code wins. Effective when the LLM is mostly right
  but noisy (atomic problems like MBPP).
* **TDR**: iterative refinement with execution-feedback critique.
  Effective when the LLM is systematically wrong but stderr teaches
  it the right shape (out-of-distribution problems like nbnative).

Hypothesis: the combination captures BOTH wins. For atomic problems,
the inner best-of-N usually finds a passing sample on the first
outer iteration (cost ~1-3 LLM calls). For out-of-distribution
problems, when best-of-N's N samples all fail, the outer TDR loop
revises with critique and tries again (cost ~4-9 LLM calls).

Cost characteristics (max_iters=3, n_samples=3):
  * Best case: 1 LLM call (first sample of first iter passes)
  * Typical: 1-3 LLM calls (one or two samples in first iter)
  * Worst case: 9 LLM calls (all N samples of all M iters fail)

Honest scope choices (carried over from the building blocks):
  * Best-of-N's inner exec uses the benchmark sandbox via
    ``run_in_subprocess`` (same as ``best_of_n.py``); production-grade
    would use ``IsolatedPyExecStep`` under ``APECX_CODE_EXEC=1``.
  * The critique format mirrors ``tdr.py`` so a future "hybrid_yaml"
    framework variant can swap in the YAML CodeWriteStep cleanly.
  * Sample independence within an iteration is "in spirit" — same
    LangChain client, same KV cache locally; depends on the LLM
    endpoint's caching behavior. Ollama serves each call statelessly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from tests.benchmarks.codegen.direct import _FENCE_PATTERN, _default_llm_for
from tests.benchmarks.codegen.tdr import _failure_summary
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.sandbox import run_in_subprocess
from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)


def _extract_code(text: str) -> str:
    if not isinstance(text, str):
        return ""
    candidates = _FENCE_PATTERN.findall(text)
    if not candidates:
        return text.strip()
    return max(candidates, key=len).strip()


def _generate_one(
    llm: Any,
    problem: BenchmarkProblem,
    *,
    previous_attempt: str | None,
    critique: str | None,
) -> str:
    """One LLM call. If previous_attempt + critique are provided
    (revision mode), include them in the prompt; otherwise just the
    spec (fresh sample mode). Mirrors the prompt shape used by
    ``tdr.py``'s _initial_codegen / _revise_codegen so hybrid +
    pure-TDR comparisons are apples-to-apples on prompt quality."""
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You write correct Python code. The user gives a problem; "
        "you respond with a single ```python fenced block containing "
        "the requested function (including all helper imports inside "
        "the block). No prose, no comments outside the block."
    )
    user_parts: list[str] = [problem.prompt.strip()]
    if problem.test_code:
        first_line = problem.test_code.splitlines()[0]
        user_parts.append(f"Your function must satisfy:\n{first_line}")
    if problem.entry_point:
        user_parts.append(f"Define a function named ``{problem.entry_point}``.")
    if previous_attempt and critique:
        user_parts.append(
            f"Previous attempt (DID NOT pass):\n```python\n{previous_attempt}\n```\n\n"
            f"Test execution feedback:\n{critique}\n\n"
            "Write a corrected version. Output only the ```python block."
        )

    response = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content="\n\n".join(user_parts))]
    )
    raw = response.content if hasattr(response, "content") else str(response)
    return _extract_code(raw)


def _exec_one(code: str, problem: BenchmarkProblem, *, timeout: float) -> tuple[bool, str, int]:
    """Run candidate in the sandbox. Returns (passed, stderr, returncode).
    Empty / unparseable code returns (False, '', -1) without raising."""
    if not code or not code.strip():
        return (False, "", -1)
    try:
        r = run_in_subprocess(
            candidate_code=code,
            setup_code=problem.setup_code or "",
            test_code=problem.test_code or "",
            timeout_seconds=timeout,
        )
        return (r.passed, r.stderr or "", r.exit_code if r.exit_code is not None else -1)
    except Exception as e:  # noqa: BLE001
        log.warning("hybrid_tdr_bon sandbox exception: %s", e)
        return (False, str(e), -1)


def make_hybrid_tdr_bon_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
    n_samples: int = 3,
    max_iters: int = 3,
    temperature: float = 0.4,
    max_tokens: int = 1024,
    exec_timeout: float = 5.0,
    llm_factory: Callable[..., Any] | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build a hybrid TDR + Best-of-N codegen.

    Args:
        n_samples: best-of-N inner sample count. Default 3 (matches
            best_of_n.py).
        max_iters: TDR outer revision count. Default 3 (matches tdr.py).
        temperature: sampling temperature. Non-zero (default 0.4) for
            best-of-N diversity. The TDR pattern usually uses temp=0
            but for hybrid we need diversity within each iteration.
        exec_timeout: per-sample subprocess timeout.

    Returns ``BenchmarkProblem -> str``. Early-returns the first
    sample that passes; on full exhaustion returns the last sample
    (so the runner records a real failure).
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

        previous_attempt: str | None = None
        critique: str | None = None
        last_sample = ""
        total_calls = 0

        for outer_iter in range(max_iters):
            # Inner best-of-N loop.
            for sample_idx in range(n_samples):
                total_calls += 1
                code = _generate_one(
                    llm,
                    problem,
                    previous_attempt=previous_attempt,
                    critique=critique,
                )
                last_sample = code
                passed, stderr, returncode = _exec_one(code, problem, timeout=exec_timeout)
                if passed:
                    log.info(
                        "hybrid_tdr_bon %s: PASSED at outer_iter=%d sample=%d (total_calls=%d)",
                        problem.problem_id,
                        outer_iter + 1,
                        sample_idx + 1,
                        total_calls,
                    )
                    return code

            # All N samples in this iteration failed. Format critique
            # from the LAST sample's stderr and feed to next iteration's
            # revision prompt.
            previous_attempt = code
            critique = _failure_summary(stderr)
            log.info(
                "hybrid_tdr_bon %s: outer_iter=%d ALL %d samples failed; "
                "preparing revision context for next iter (total_calls=%d)",
                problem.problem_id,
                outer_iter + 1,
                n_samples,
                total_calls,
            )

        log.info(
            "hybrid_tdr_bon %s: exhausted %d iters * %d samples = %d calls "
            "without pass — returning last sample",
            problem.problem_id,
            max_iters,
            n_samples,
            total_calls,
        )
        return last_sample

    return _codegen


__all__ = ["make_hybrid_tdr_bon_codegen"]
