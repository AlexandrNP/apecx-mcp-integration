"""Best-of-N direct codegen — multiple independent samples + exec-rank.

G103 (2026-05-17). Generates ``n_samples`` independent direct
codegen responses with non-zero temperature, then runs each against
the problem's ``test_code`` in the same sandbox the runner uses,
and returns the first one that passes. If none passes, returns the
last one (so the runner records a real failure, not a synthesized
one).

Hypothesis (from G101 failure-mode taxonomy): TDR helps where
revision can recover; best-of-N helps where the LLM has occasional
"good by luck" attempts that fail at temp=0 but succeed at higher
temperature. Different mechanism from TDR — sampling diversity vs
execution-feedback revision.

Cost characteristics:
  * ~n_samples × direct LLM cost
  * + n_samples × subprocess exec (cheap; ~50ms each)
  * No revision loop, no recursive decomposition

Honest scope caveats:
  * Higher temperature increases noise. The "diversity payoff"
    depends on the LLM's reasoning being multi-modal on the
    problem (i.e., it has multiple distinct plausible approaches).
    For well-known patterns the LLM converges to the same answer
    regardless of temperature — best-of-N adds cost without
    benefit.
  * Independence is "in spirit": all N samples see the same prompt
    and the LLM's KV cache may bias subsequent calls in the same
    process. Ollama serves each call statelessly so this is moot;
    in deployments with explicit caching, the diversity may be less
    than expected.
  * Reuses the SAME sandbox path as the benchmark runner
    (subprocess via ``tests.benchmarks.sandbox.run_in_subprocess``).
    Means we use the benchmark-side sandbox, not IsolatedPyExecStep
    (which is opt-in-gated). For a production-grade pattern, the
    same code with IsolatedPyExecStep substitution would apply.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from tests.benchmarks.codegen.direct import _FENCE_PATTERN, _default_llm_for
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.sandbox import run_in_subprocess
from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)


def _extract_code(text: str) -> str:
    """Pull a fenced ```python block (largest one) from the LLM
    response, or return the raw text stripped if no fence. Matches
    the behavior of the direct codegen so a parity comparison stays
    meaningful."""
    if not isinstance(text, str):
        return ""
    candidates = _FENCE_PATTERN.findall(text)
    if not candidates:
        return text.strip()
    return max(candidates, key=len).strip()


def _generate_one(
    llm: Any,
    problem: BenchmarkProblem,
) -> str:
    """One independent direct codegen call. Same prompt shape as
    ``direct.py``'s ``_codegen`` so the only deliberate variable is
    the sampling temperature."""
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You write correct Python code. The user gives a problem; "
        "you respond with a single ```python fenced block containing "
        "the requested function (including all helper imports inside "
        "the block). No prose, no comments outside the block."
    )
    user_parts = [problem.prompt.strip()]
    if problem.test_code:
        first_line = problem.test_code.splitlines()[0]
        user_parts.append(f"Your function must satisfy:\n{first_line}")
    if problem.entry_point:
        user_parts.append(f"Define a function named ``{problem.entry_point}``.")

    response = llm.invoke(
        [
            SystemMessage(content=system),
            HumanMessage(content="\n\n".join(user_parts)),
        ]
    )
    raw = response.content if hasattr(response, "content") else str(response)
    return _extract_code(raw)


def _exec_against_tests(
    code: str,
    problem: BenchmarkProblem,
    *,
    timeout: float,
) -> bool:
    """Run candidate code against problem's test_code in a subprocess.
    Returns True iff exec_succeeded. Empty / unparseable code returns
    False without raising.
    """
    if not code or not code.strip():
        return False
    try:
        result = run_in_subprocess(
            candidate_code=code,
            setup_code=problem.setup_code or "",
            test_code=problem.test_code or "",
            timeout_seconds=timeout,
        )
        # SandboxResult.passed is True iff subprocess exited 0.
        return result.passed
    except Exception as e:
        # The runner expects a bool decision here, not an exception —
        # any sandbox-side failure is "this sample didn't pass". The
        # downstream runner will surface its own subprocess result on
        # the returned-code's final run, capturing the real error
        # class.
        log.warning("Best-of-N sample exec failed: %s", e)
        return False


def make_best_of_n_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
    n_samples: int = 3,
    temperature: float = 0.4,
    max_tokens: int = 1024,
    exec_timeout: float = 5.0,
    llm_factory: Callable[..., Any] | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build a best-of-N codegen function bound to a model + base_url.

    Args:
        model / base_url: as in direct.py — resolved through
            ``resolve_role("drafter", ...)``.
        n_samples: number of independent samples per problem. Default 3
            — sweet spot between cost (3× direct) and diversity (enough
            samples to see multi-modal LLM behavior on hard problems).
        temperature: sampling temperature. Higher than direct's 0 to
            encourage diversity. Default 0.4 — empirically a balance
            between "samples are too similar" and "samples are
            garbage". Operators can tune per model.
        max_tokens: max response length. Same as direct.
        exec_timeout: per-sample subprocess timeout. Default 5s. The
            sandbox kills runaway samples without blocking the loop.
        llm_factory: optional injection point for tests.

    Returns ``BenchmarkProblem -> str``. The string is the first
    sample whose test_code passed in the sandbox, or the last sample
    if none passed (the benchmark runner will record the real failure
    on its own final exec call).
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

        last_sample = ""
        for i in range(n_samples):
            log.info(
                "best_of_n sample %d/%d for problem %s",
                i + 1,
                n_samples,
                problem.problem_id,
            )
            sample = _generate_one(llm, problem)
            last_sample = sample
            if _exec_against_tests(sample, problem, timeout=exec_timeout):
                log.info(
                    "best_of_n sample %d/%d PASSED for %s — returning early",
                    i + 1,
                    n_samples,
                    problem.problem_id,
                )
                return sample

        log.info(
            "best_of_n: all %d samples failed for %s — returning last "
            "(runner will record the real failure class)",
            n_samples,
            problem.problem_id,
        )
        return last_sample

    return _codegen


__all__ = ["make_best_of_n_codegen"]
