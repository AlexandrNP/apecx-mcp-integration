"""Plan-then-code codegen scaffold.

Two LLM round-trips:

1. **Planner** — nemotron-3-nano:4b (or whichever ``planner`` role
   model is configured). Produces a numbered-list plan of how to
   solve the problem. Nemotron's chain-of-thought is captured in
   ``<think>...</think>`` tokens which we strip before passing the
   plan to the drafter.

2. **Drafter** — mistral-nemo:latest (or whichever ``drafter`` role
   model is configured). Receives the problem prompt + the plan,
   emits a Python code fenced block.

The user's framework directive in this workspace is to express
multi-LLM scaffolds as nanobrain workflows. We keep this codegen
procedural so the benchmark suite can iterate fast; the nanobrain
workflow wrap lives in
``apecx_integration.composition.workflows.benchmark_plan_then_code``
and shares the same prompt strings.

Per ``docs/composer_benchmark_plan.md`` — this is the scaffold A
intervention measured against the direct-codegen baseline.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from tests.benchmarks.codegen.direct import _default_llm_for, _extract_code
from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.types import BenchmarkProblem

# Strip the nemotron / qwen / deepseek-style <think>...</think>
# block out of a planner response. Some models emit reasoning then
# answer; we only want the answer.
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def make_plan_then_code_codegen(
    *,
    planner_model: str | None = None,
    drafter_model: str | None = None,
    base_url: str | None = None,
    planner_temperature: float = 0.0,
    drafter_temperature: float = 0.0,
    max_tokens: int = 1024,
) -> Callable[[BenchmarkProblem], str]:
    """Build the plan-then-code codegen.

    Resolution: delegated to ``resolve_role(...)`` per stage —
    explicit kwarg > APECX_LLM_MODEL_<ROLE> > composer_config.yml
    model_roles.<role> > APECX_LLM_MODEL > hardcoded default.
    Each stage resolves its own base_url so an operator can route
    planner to a different Ollama host than drafter if needed.
    """
    planner_resolved, planner_base = resolve_role(
        "planner",
        kwarg_model=planner_model,
        kwarg_base_url=base_url,
    )
    drafter_resolved, drafter_base = resolve_role(
        "drafter",
        kwarg_model=drafter_model,
        kwarg_base_url=base_url,
    )

    def _codegen(problem: BenchmarkProblem) -> str:
        plan = _run_planner(
            problem, planner_resolved, planner_base, planner_temperature, max_tokens
        )
        return _run_drafter(
            problem, plan, drafter_resolved, drafter_base, drafter_temperature, max_tokens
        )

    return _codegen


def _run_planner(
    problem: BenchmarkProblem,
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Ask the planner to produce a numbered plan.

    The plan is text, not code. We deliberately do NOT ask the
    planner to write code — separation of roles is the whole point.
    The think-block stripper handles models that emit reasoning
    in ``<think>...</think>``.
    """
    llm = _default_llm_for(model, base_url, temperature, max_tokens)
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    system = (
        "You are a code-planner. The user gives a Python problem. "
        "You respond with a SHORT numbered plan (3-6 steps max) "
        "describing how to solve it. You do NOT write code. You do "
        "NOT include any prose other than the numbered plan."
    )
    user_parts = [problem.prompt.strip()]
    if problem.test_code:
        first_line = problem.test_code.splitlines()[0]
        user_parts.append(f"Example test: {first_line}")
    if problem.entry_point:
        user_parts.append(f"The function is named ``{problem.entry_point}``.")

    response = llm.invoke(
        [
            SystemMessage(content=system),
            HumanMessage(content="\n\n".join(user_parts)),
        ]
    )
    raw = response.content if hasattr(response, "content") else str(response)
    return _THINK_BLOCK.sub("", raw).strip()


def _run_drafter(
    problem: BenchmarkProblem,
    plan: str,
    model: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Ask the drafter to emit code using the plan as guidance.

    Same prompt shape as the direct codegen, with one extra block
    carrying the plan. The drafter is told to USE the plan but is
    not required to follow it slavishly — sometimes the plan misses
    an edge case and the drafter has to improvise.
    """
    llm = _default_llm_for(model, base_url, temperature, max_tokens)
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    system = (
        "You write correct Python code. The user gives a problem AND "
        "a suggested plan from a planning step. Use the plan as "
        "guidance but feel free to adjust if you spot a flaw. "
        "Respond with a single ```python``` fenced block containing "
        "the requested function (including any imports inside the "
        "block). No prose, no comments outside the block."
    )
    user_parts = [
        problem.prompt.strip(),
        f"Suggested plan:\n{plan}",
    ]
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


__all__ = ["make_plan_then_code_codegen"]
