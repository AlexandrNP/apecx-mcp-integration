"""Self-consistency vote codegen — G108 (2026-05-17).

Generate N independent samples at non-zero temperature; pick the
one with the most AST-similar peers. No execution oracle required —
unlike best_of_n, this works even when test_code is unavailable or
when the sandbox is expensive.

Mechanism: pairwise AST-similarity scoring. For each sample, count
how many other samples share the same normalized AST structure
(function names, statement types, control-flow shape). The sample
with the most peers is the "majority answer". Ties broken by
sample order (first wins) — deterministic.

When to use this vs best_of_n:
  * best_of_n needs a fast, reliable execution oracle (test_code +
    sandbox). When the test_code is missing OR the sandbox costs a
    lot per call, self-consistency is the next-best signal.
  * self-consistency assumes "most samples are correct" — works
    when the LLM converges to the right answer most of the time
    and the wrong answers are diverse (uncorrelated). Fails when
    the LLM is systematically wrong (then majority IS wrong).

Hypothesis: weaker signal than best_of_n on datasets with reliable
test_code (MBPP), but useful for datasets without (e.g., free-form
generation tasks). Empirical validation against MBPP will be a
lower bound — expect best_of_n to outperform self_consistency
there.

Cost: ~N× direct LLM, no sandbox calls. Cheaper than best_of_n
when sandbox spinup > AST-parse cost (which is always).
"""

from __future__ import annotations

import ast
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from tests.benchmarks.codegen.direct import _FENCE_PATTERN, _default_llm_for
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)


def _extract_code(text: str) -> str:
    if not isinstance(text, str):
        return ""
    candidates = _FENCE_PATTERN.findall(text)
    if not candidates:
        return text.strip()
    return max(candidates, key=len).strip()


def _normalized_ast_signature(code: str) -> str | None:
    """Reduce a code string to an AST-shape signature: a tuple of
    statement-type names + function names. Returns None when the
    code doesn't parse (will be excluded from voting).

    Why this and not full AST equality: full equality is too strict
    (whitespace + comments + variable renames break it). Type-shape
    is too loose (every function with one return statement looks
    the same). The middle ground: function name + statement-type
    sequence captures "two samples that compute the same thing in
    the same way".
    """
    if not code or not code.strip():
        return None
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    parts: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Function: name + per-statement type names.
            body_types = ",".join(type(n).__name__ for n in node.body)
            parts.append(f"def:{node.name}({body_types})")
        else:
            parts.append(type(node).__name__)
    return ";".join(parts)


def _vote(samples: list[str]) -> int:
    """Return the index of the sample with the most peers sharing
    its AST signature. Samples that don't parse don't get votes;
    when all samples don't parse, falls back to index 0 (the runner
    will surface the SyntaxError on the final exec).

    Tie-breaker: first sample in the majority group wins (stable +
    deterministic across runs).
    """
    sigs: list[str | None] = [_normalized_ast_signature(s) for s in samples]
    sig_counts: Counter[str] = Counter(s for s in sigs if s is not None)
    if not sig_counts:
        return 0
    # Most common signature.
    winning_sig, _ = sig_counts.most_common(1)[0]
    for i, sig in enumerate(sigs):
        if sig == winning_sig:
            return i
    return 0  # Unreachable but defensive.


def _generate_one(llm: Any, problem: BenchmarkProblem) -> str:
    """Same prompt shape as direct.py's _codegen for parity."""
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
        [SystemMessage(content=system), HumanMessage(content="\n\n".join(user_parts))]
    )
    raw = response.content if hasattr(response, "content") else str(response)
    return _extract_code(raw)


def make_self_consistency_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
    n_samples: int = 3,
    temperature: float = 0.4,
    max_tokens: int = 1024,
    llm_factory: Callable[..., Any] | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build a self-consistency vote codegen function.

    Args:
        n_samples: number of samples per problem. Default 3 — three
            samples is the minimum for a meaningful "majority"; with
            two, ties dominate. Larger N gives better signal at
            higher cost.
        temperature: sampling temperature. Default 0.4 — same as
            best_of_n.py for cross-pattern comparability.
        max_tokens: max response length.
        llm_factory: optional injection point for tests.

    Returns ``BenchmarkProblem -> str``. Always returns one of the N
    samples (the majority winner by AST shape). Never modifies the
    samples — verbatim selection so the benchmark runner records
    real LLM output.
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

        samples: list[str] = []
        for i in range(n_samples):
            log.info(
                "self_consistency %s: sample %d/%d",
                problem.problem_id,
                i + 1,
                n_samples,
            )
            samples.append(_generate_one(llm, problem))

        winning_idx = _vote(samples)
        log.info(
            "self_consistency %s: winner = sample %d (of %d) by AST-signature majority",
            problem.problem_id,
            winning_idx + 1,
            n_samples,
        )
        return samples[winning_idx]

    return _codegen


__all__ = ["make_self_consistency_codegen"]
