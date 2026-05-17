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

import ast
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


def _rename_entry_function_if_needed(code: str, requested_name: str | None) -> str:
    """If ``code`` defines a function whose name doesn't match
    ``requested_name``, rename it. AST-based so it's safe across
    arbitrary indentation / formatting.

    Why this is a separate post-processing pass (G102b, 2026-05-17):
    The G101 failure-mode analysis attributed HD-RSS's 17 MBPP
    NameErrors to the LLM composer dropping helpers. The G102 fix
    (templated composer) didn't move the needle because the actual
    bug was in ``_atomic_codegen``: the LLM normalizes function names
    to lowercase snake_case regardless of the requested entry_point.
    Verified by inspecting failing samples in
    ``/tmp/bench_mbpp_hd_rss_v2_n100.json`` — every NameError case
    had a correctly-implemented function defined under the wrong
    name. The post-hoc rename closes this gap robustly, regardless
    of whether the LLM honored the entry_point hint in the prompt.

    Heuristic: when the code defines exactly one top-level function,
    rename it. When it defines multiple, rename the LAST one (which
    is conventionally the orchestrator/entry point). When the code
    doesn't parse, return as-is — caller will hit the SyntaxError
    in the sandbox and the runner will surface the real error class.

    Args:
        code: candidate source from the LLM.
        requested_name: the entry point the test_code will call. If
            None, no rename — return code unchanged.

    Returns:
        Source with the entry function renamed iff a rename was
        applied; otherwise the original source.
    """
    if not requested_name or not isinstance(code, str) or not code.strip():
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    func_nodes = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not func_nodes:
        return code

    # The orchestrator/entry function is conventionally last in the
    # module body. If it already matches, no-op.
    entry_node = func_nodes[-1]
    current_name = entry_node.name
    if current_name == requested_name:
        return code

    # Rename via regex on the source: targets ``def <current>`` (with
    # optional whitespace before paren). AST-based code-rewrite would
    # be more robust (handle multi-line decorators etc.) but for the
    # common case of a simple ``def name(args):`` line a regex is
    # adequate. We deliberately do NOT rename CALL sites of the
    # function — the orchestrator pattern is that the entry function
    # is called from the test_code (outside our scope), not from
    # within the candidate's own source.
    pattern = re.compile(rf"\bdef\s+{re.escape(current_name)}\s*\(", re.MULTILINE)
    new_code, n_subs = pattern.subn(f"def {requested_name}(", code, count=1)
    if n_subs == 0:
        return code

    log.info(
        "hd_rss entry-point rename: %r -> %r (1 occurrence)",
        current_name,
        requested_name,
    )
    return new_code


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
    """Generate code for an atomic problem — no decomposition.

    G102b (2026-05-17): the result is post-processed via
    ``_rename_entry_function_if_needed`` so the function defined in
    the LLM's output ALWAYS matches the requested entry_point. Before
    this fix, the LLM frequently normalized the function name to
    lowercase snake_case regardless of the prompt instruction —
    that was the actual root cause of HD-RSS's 17 MBPP NameErrors
    (G101 mis-attributed it to the composer).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You write a single Python function. Respond with one "
        "```python fenced block. Include any imports inside the "
        "block. No prose. The function name MUST match exactly what "
        "the user requests (case-sensitive, character-for-character)."
    )
    user_parts = [description.strip()]
    if entry_point:
        user_parts.append(
            f"Define a function named EXACTLY ``{entry_point}`` "
            "(case-sensitive; do not lowercase or rename)."
        )
    response = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content="\n\n".join(user_parts))]
    )
    code = _extract_code(response.content)
    return _rename_entry_function_if_needed(code, entry_point)


def _compose(
    llm: Any,
    original_problem: str,
    subsolutions: list[dict[str, str]],
    parent_entry_point: str | None,
) -> str:
    """Compose subsolutions into a final top-level function (LLM composer).

    The composer LLM call sees: the original problem statement, each
    sub-function's code (already generated recursively), and the
    expected top-level entry point. It writes the top-level function
    that orchestrates the subs AND re-emits the helpers verbatim.

    Known failure mode (per G101): on weak LLMs (mistral-nemo) the
    composer drops helper definitions while still referencing them,
    yielding NameError at runtime. ``_compose_templated`` below
    eliminates this failure mode by concatenating helpers
    deterministically and asking the LLM to write ONLY the top-level
    function — see G102.
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
    # G102b: defense-in-depth — if the LLM still renamed the entry,
    # snap it back to the requested name.
    return _rename_entry_function_if_needed(_extract_code(response.content), parent_entry_point)


def _compose_templated(
    llm: Any,
    original_problem: str,
    subsolutions: list[dict[str, str]],
    parent_entry_point: str | None,
) -> str:
    """Compose subsolutions via deterministic helper-concat + LLM glue (G102).

    Trade-off vs ``_compose`` (LLM composer):
      * Helpers are guaranteed present (concatenated verbatim by the
        codegen wrapper, not re-emitted by the LLM). Eliminates the
        17 MBPP NameErrors + 4 nbnative AttributeErrors that G101
        identified as the LLM composer's distinctive failure mode.
      * The LLM's surface area shrinks: it writes ONE function (the
        orchestrator) instead of ONE function + N re-emitted helpers.
        Smaller surface → fewer chances to drop or rename a helper.
      * Cost: same number of LLM calls per problem as ``_compose``
        (one); but each call's prompt + response is smaller because
        the helpers aren't being re-emitted.

    The LLM is told:
      * Here are the names + signatures + (implicit) bodies of the
        pre-defined helpers.
      * Write ONLY ``def <parent>(...)`` that calls these helpers.
      * Do NOT redefine any helper.

    The wrapper then prepends the helper bodies + appends the LLM's
    output. Result: helpers always defined; orchestrator is the
    LLM's only contribution.

    Honest scope limitations:
      * The LLM still has to figure out the correct CALL pattern
        (which helper to call with what args, in what order). If the
        decomposition produced helpers that don't naturally compose
        for the problem, the LLM's orchestrator will be wrong — that
        manifests as AssertionError, not NameError. So we've shifted
        the failure mode from "name missing" to "wrong logic",
        which is at least debuggable.
      * The helper bodies are NOT shown in the LLM prompt — only
        the names + a one-line description (the original subgoal
        description). This further shrinks the prompt but means the
        LLM has to trust the helper signatures.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # Build a short helper INDEX (name + description) for the LLM —
    # not the full bodies. Bodies will be concatenated in by the
    # wrapper, not seen by the LLM.
    helper_index_lines: list[str] = []
    for sub in subsolutions:
        desc = sub.get("description", "").strip().replace("\n", " ")
        # Truncate very long descriptions for prompt economy.
        if len(desc) > 200:
            desc = desc[:200] + "..."
        helper_index_lines.append(f"- ``{sub['name']}`` — {desc}")
    helper_index = "\n".join(helper_index_lines)

    system = (
        "You write ONE Python function that orchestrates pre-defined "
        "helper functions to solve the original problem. The helpers "
        "are ALREADY defined and will be available in the same module. "
        "DO NOT redefine them. Output ONLY the top-level function "
        "definition, in a single ```python fenced block. No prose, "
        "no helper definitions, no import statements."
    )
    user_parts = [
        f"Original problem:\n{original_problem.strip()}",
        f"Available helpers (already defined; call them by name):\n{helper_index}",
    ]
    if parent_entry_point:
        user_parts.append(
            f"Top-level function name: ``{parent_entry_point}``. Output: "
            f"just ``def {parent_entry_point}(...):`` and its body."
        )
    else:
        user_parts.append(
            "Top-level function name: pick a sensible one. Output: just the ``def`` and its body."
        )

    response = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content="\n\n".join(user_parts))]
    )
    orchestrator_code = _extract_code(response.content)

    # G102b: snap orchestrator name to requested entry_point if the
    # LLM renamed it (defense in depth — the prompt asks but the LLM
    # doesn't always obey).
    orchestrator_code = _rename_entry_function_if_needed(orchestrator_code, parent_entry_point)

    # Deterministic assembly: helpers (verbatim) + orchestrator.
    helper_bodies = "\n\n".join(sub["code"].rstrip() for sub in subsolutions)
    if helper_bodies:
        return f"{helper_bodies}\n\n{orchestrator_code}"
    return orchestrator_code


def _solve_recursive(
    llm: Any,
    description: str,
    entry_point: str | None,
    depth: int,
    composer_strategy: str = "llm",
) -> str:
    """The recursive core. Returns the code string for the given
    problem description.

    Termination guarantees:
      * Hard depth cap forces atomic at depth ``_MAX_RECURSION_DEPTH``.
      * Per-level subgoal cap of 4 bounds branching factor.
      * Worst-case calls: 4^3 + 4^2 + 4 = 84 LLM calls at max depth.

    ``composer_strategy`` (G102):
      * ``"llm"`` (default, original HD-RSS) — LLM composer re-emits
        helpers + writes orchestrator. Surfaces NameError on weak
        models when the LLM drops a helper definition.
      * ``"templated"`` — deterministic helper concat + LLM-written
        orchestrator only. Eliminates NameErrors at the cost of
        shifting the failure mode toward "wrong orchestrator logic"
        (which manifests as AssertionError).
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
            composer_strategy=composer_strategy,
        )

    log.info(
        "%sHD-RSS depth=%d: composing %d subsolutions via %s",
        indent,
        depth,
        len(subgoals),
        composer_strategy,
    )
    if composer_strategy == "templated":
        return _compose_templated(llm, description, subgoals, entry_point)
    return _compose(llm, description, subgoals, entry_point)


def make_hd_rss_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    llm_factory: Callable[..., Any] | None = None,
    composer_strategy: str = "llm",
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
            composer_strategy=composer_strategy,
        )

    return _codegen
