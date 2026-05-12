"""Direct LLM codegen — single round-trip, no scaffold.

This is the baseline. Every multi-step scaffold (plan-then-code,
review-revise, self-test) is measured against this floor.

The prompt is deliberately minimal:

  System: "You write Python. Return ONLY a ```python``` fenced
           block with the requested function. No prose."
  User:   "<problem.prompt>\n\nExample test: <first test line>"

We do NOT inject framework boilerplate. For pure-Python benchmarks
(MBPP, SciCode subproblems) the function body IS the answer; the
nanobrain-native benchmark uses a separate codegen that emits
workflow YAML.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from tests.benchmarks.model_roles import resolve_role
from tests.benchmarks.types import BenchmarkProblem

_FENCE_PATTERN = re.compile(
    r"```(?:python|py)?\s*\n(.*?)\n\s*```",
    re.DOTALL,
)

# Lazy-instantiated LLM client. The factory cost (~0.3s) is paid
# once per process. Tests inject their own factory.
_LLM_CACHE: dict[tuple[str, str, float, int], Any] = {}


def _default_llm_for(model: str, base_url: str, temperature: float, max_tokens: int) -> Any:
    """Get-or-build a ChatOpenAI for the given (model, base_url) tuple.

    Cached per-process — building LangChain wrappers is non-free.
    """
    key = (model, base_url, temperature, max_tokens)
    if key in _LLM_CACHE:
        return _LLM_CACHE[key]
    from apecx_integration.agents._llm_factory import build_chat_llm  # noqa: PLC0415

    llm = build_chat_llm(
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        base_url=base_url,
    )
    _LLM_CACHE[key] = llm
    return llm


def make_direct_codegen(
    *,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    llm_factory: Callable[..., Any] | None = None,
) -> Callable[[BenchmarkProblem], str]:
    """Build a direct-LLM codegen function bound to a specific model.

    The returned function is what ``runner.run_one`` consumes. It
    raises on LLM failure (the runner buckets that as
    ``codegen_<ExceptionType>``).

    Resolution: delegated to ``resolve_role("drafter", ...)`` —
    explicit kwargs > APECX_LLM_MODEL_DRAFTER > composer_config.yml
    model_roles.drafter > APECX_LLM_MODEL > hardcoded default.
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

        from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

        response = llm.invoke(
            [
                SystemMessage(content=system),
                HumanMessage(content="\n\n".join(user_parts)),
            ]
        )

        raw = response.content if hasattr(response, "content") else str(response)
        return _extract_code(raw)

    return _codegen


def _extract_code(raw: str) -> str:
    """Pull the largest ```python ... ``` block from the response.

    LLMs sometimes emit multiple fences (helper module, then the
    main function); pick the largest to maximize the chance of
    grabbing the real answer. If no fence, return the whole
    response — the sandbox will surface a SyntaxError if it's
    unparseable, which is the right outcome.
    """
    candidates = _FENCE_PATTERN.findall(raw)
    if not candidates:
        return raw
    return max(candidates, key=len)


__all__ = ["make_direct_codegen"]
