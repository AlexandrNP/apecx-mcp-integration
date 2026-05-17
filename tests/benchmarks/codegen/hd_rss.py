"""Hierarchical Decomposition with Recursive Subgoal Solving (HD-RSS) — G98 (2026-05-17).

A novel synthesis NOT in the 3 surveyed papers as a single named
pattern. The Wei survey lists "hierarchical task decomposition" as a
generic category; HD-RSS specifies the RECURSIVE call shape that
makes it concrete:

  solve(problem):
      if LLM judges problem atomic:
          return LLM.write_code(problem)
      else:
          subgoals = LLM.decompose(problem)
          subsolutions = [solve(sg) for sg in subgoals]   # ← recurse
          return LLM.compose(subsolutions, problem)

Why HD-RSS over the existing patterns
=====================================

The project's ``plan_then_code`` is **flat 2-stage** (plan → code).
HD-RSS is **N-level recursive** — each subgoal can itself be
decomposed further until reaching atomic operations the LLM can
handle in one shot. The composition step then bottom-up assembles
solutions.

The literature has hierarchical RL with subgoal discovery, but
LLM-based code-gen hasn't widely adopted the RECURSIVE shape
(decomposition usually stops at depth 1-2). HD-RSS tests whether
deeper recursion helps for compositional code-gen.

Honest scope choices
====================

* **Atomicity is LLM self-judged.** We ask the LLM "is this a
  single-function problem you can implement in <30 lines?" — if
  yes, codegen; if no, decompose. This is unreliable for a weak
  LLM (mistral-nemo will sometimes say "yes" for over-complex
  problems and "no" for trivial ones). A future v2 should use a
  more concrete atomicity heuristic (e.g., AST size of a draft).
* **Maximum recursion depth is hard-capped at 3.** Without a cap,
  a confused LLM can produce subgoals that re-decompose forever.
  Cap is per-instance; depth-0 is the top-level call. Going beyond
  3 levels is rare on MBPP-scale problems.
* **No memoization.** Sibling subgoals at the same level might
  invoke the LLM with overlapping content. We don't cache. Adding
  cache is straightforward but adds complexity.
* **Composition is also LLM-driven.** Given the original problem +
  the N sub-solutions, ask the LLM to write the top-level function
  that calls the sub-functions. The composition can introduce its
  own bugs (wrong glue logic), which is a known weakness.

Why nanobrain-framework-native (per workspace rule)
====================================================

Like TDR (G93), the LLM calls go through ``build_chat_llm`` and the
sandbox is shared. The **recursion itself** is Python because
nanobrain's existing primitives (LoopController = iteration not
recursion; SubworkflowStep = static config-time loading not dynamic
recursion) don't support true workflow recursion. True recursive
workflow self-reference would be a NEW framework primitive
(``RecursiveSubworkflowStep`` or similar); proposing it lives in
docs/hd_rss_pattern_2026-05-17.md.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from tests.benchmarks.codegen.direct import _FENCE_PATTERN, _default_llm_for
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.types import BenchmarkProblem

log = logging.getLogger(__name__)

# Hard cap on recursion depth — protects against runaway recursion
# from a confused LLM that keeps saying "not atomic" on simple problems.
_MAX_RECURSION_DEPTH = 3

# Cap on the number of subgoals an LLM is allowed to emit per
# decomposition step. Prevents quadratic explosion when an LLM
# over-decomposes (e.g., "10 trivial sub-functions" for a problem
# that needs 2).
_MAX_SUBGOALS_PER_LEVEL = 4


def _extract_code(text: str) -> str:
    """Extract Python code from a fenced ```python block, or return
    the whole text stripped if no fence."""
    m = _FENCE_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _judge_atomic(
    llm: Any,
    problem_description: str,
    depth: int,
) -> bool:
    """Ask the LLM: is this problem atomic (one-function-implementable)
    or should it be decomposed?

    Hard-truncates to atomic when ``depth >= _MAX_RECURSION_DEPTH``
    regardless of LLM judgment — protects against runaway recursion.
    """
    if depth >= _MAX_RECURSION_DEPTH:
        log.info("HD-RSS: depth cap %d reached; forcing atomic", _MAX_RECURSION_DEPTH)
        return True

    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You judge whether a coding problem is ATOMIC (one Python "
        "function, ≤30 lines) or COMPOSITE (needs to be broken into "
        "multiple helper functions). Respond with EXACTLY one word: "
        "ATOMIC or COMPOSITE. No explanation."
    )
    user = f"Problem:\n{problem_description.strip()}\n\nAnswer (one word):"

    response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    verdict = response.content.strip().upper()
    # Be lenient with the parsing — LLMs sometimes add extra words.
    if "ATOMIC" in verdict:
        return True
    if "COMPOSITE" in verdict:
        return False
    # Ambiguous response — treat as atomic to avoid runaway recursion.
    log.warning("HD-RSS: ambiguous atomicity verdict %r; treating as atomic", verdict[:80])
    return True


def _decompose(
    llm: Any,
    problem_description: str,
    parent_entry_point: str | None,
) -> list[dict[str, str]]:
    """Ask the LLM to decompose a problem into 2-4 named subgoals,
    each with a description.

    Returns a list of ``{name, description}`` dicts. Empty list on
    parse failure (caller treats as atomic).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You decompose a coding problem into 2-4 helper functions. "
        "Respond with a numbered list. Each line: "
        "``N. <function_name>: <one-sentence description of what it does>``. "
        "Helpers must be self-contained — each describable without "
        "referencing the others. The top-level function will call "
        "them in sequence/combination. No prose outside the list."
    )
    user_parts = [
        f"Problem:\n{problem_description.strip()}",
    ]
    if parent_entry_point:
        user_parts.append(
            f"The top-level function will be named ``{parent_entry_point}``. "
            "Do NOT include it in your decomposition — only the helpers."
        )

    response = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content="\n\n".join(user_parts))]
    )
    text = response.content.strip()

    # Parse numbered list: "1. name: description"
    pattern = re.compile(r"^\s*\d+\.\s*([A-Za-z_][\w]*)\s*:\s*(.+)$", re.MULTILINE)
    matches = pattern.findall(text)
    if not matches:
        log.warning("HD-RSS: decomposition parse failed; raw response: %r", text[:200])
        return []
    subgoals = [{"name": name, "description": desc.strip()} for name, desc in matches]
    # Apply per-level cap.
    return subgoals[:_MAX_SUBGOALS_PER_LEVEL]


def _atomic_codegen(
    llm: Any,
    description: str,
    entry_point: str | None,
) -> str:
    """Generate code for an atomic problem — no decomposition."""
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You write a single Python function. Respond with one "
        "```python fenced block. Include any imports inside the "
        "block. No prose."
    )
    user_parts = [description.strip()]
    if entry_point:
        user_parts.append(f"Define a function named ``{entry_point}``.")
    response = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content="\n\n".join(user_parts))]
    )
    return _extract_code(response.content)


def _compose(
    llm: Any,
    original_problem: str,
    subsolutions: list[dict[str, str]],
    parent_entry_point: str | None,
) -> str:
    """Compose subsolutions into a final top-level function.

    The composer LLM call sees: the original problem statement, each
    sub-function's code (already generated recursively), and the
    expected top-level entry point. It writes the top-level function
    that orchestrates the subs.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    helper_blocks = "\n\n".join(
        f"# Helper: {sub['name']}\n```python\n{sub['code']}\n```" for sub in subsolutions
    )

    system = (
        "You compose helper functions into a top-level Python function "
        "that solves the original problem. INCLUDE all helper code in "
        "your response (paste them verbatim, then add the top-level "
        "function). Respond with one ```python fenced block. No prose."
    )
    user_parts = [
        f"Original problem:\n{original_problem.strip()}",
        f"Helpers (already implemented):\n{helper_blocks}",
    ]
    if parent_entry_point:
        user_parts.append(f"Top-level function name: ``{parent_entry_point}``.")
    user_parts.append(
        "Output: a single ```python block containing ALL helpers + the top-level function."
    )
    response = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content="\n\n".join(user_parts))]
    )
    return _extract_code(response.content)


def _solve_recursive(
    llm: Any,
    description: str,
    entry_point: str | None,
    depth: int,
) -> str:
    """The recursive core. Returns the code string for the given
    problem description.

    Termination guarantees:
      * Hard depth cap forces atomic at depth ``_MAX_RECURSION_DEPTH``.
      * Per-level subgoal cap of 4 bounds branching factor.
      * Worst-case calls: 4^3 + 4^2 + 4 = 84 LLM calls at max depth.
    """
    indent = "  " * depth
    log.info("%sHD-RSS depth=%d: judging atomicity for: %s", indent, depth, description[:80])

    if _judge_atomic(llm, description, depth):
        log.info("%sHD-RSS depth=%d: ATOMIC — direct codegen", indent, depth)
        return _atomic_codegen(llm, description, entry_point)

    subgoals = _decompose(llm, description, entry_point)
    if not subgoals:
        # Parse failure — fall back to atomic.
        log.info("%sHD-RSS depth=%d: empty decomposition — fall back to atomic", indent, depth)
        return _atomic_codegen(llm, description, entry_point)

    log.info(
        "%sHD-RSS depth=%d: COMPOSITE; %d subgoals: %s",
        indent,
        depth,
        len(subgoals),
        [sg["name"] for sg in subgoals],
    )

    # Recursive solve for each subgoal.
    for sub in subgoals:
        # Each subgoal becomes its own atomic-or-composite call.
        # Pass the subgoal name as its entry point so the LLM writes
        # ``def <subgoal_name>(...)``.
        sub["code"] = _solve_recursive(
            llm,
            description=sub["description"],
            entry_point=sub["name"],
            depth=depth + 1,
        )

    log.info("%sHD-RSS depth=%d: composing %d subsolutions", indent, depth, len(subgoals))
    return _compose(llm, description, subgoals, entry_point)


def make_hd_rss_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    llm_factory: Callable[..., Any] | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build an HD-RSS codegen function bound to a specific model.

    Returns a callable ``BenchmarkProblem -> str``. The returned code
    is the LLM's final composed solution at depth-0; the runner
    re-executes to make the final pass/fail call.

    Cost characteristics (worst case at max recursion depth 3 + max
    subgoals 4 per level):
      * Atomicity judgements: 4^0 + 4^1 + 4^2 = 21 calls
      * Atomic codegens:      4^3 = 64 calls
      * Decompositions:       4^0 + 4^1 = 5 calls
      * Compositions:         4^0 + 4^1 = 5 calls
      * TOTAL WORST CASE:    ~95 LLM calls per problem
    In practice typical problems atomic-out at depth 0 or 1 with 0-3
    subgoals each, yielding 3-15 LLM calls per problem.
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

        return _solve_recursive(
            llm,
            description=problem.prompt,
            entry_point=problem.entry_point or None,
            depth=0,
        )

    return _codegen
