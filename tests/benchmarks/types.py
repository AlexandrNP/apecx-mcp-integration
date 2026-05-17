"""Shared dataclasses for the benchmark harness.

Kept deliberately small. ``BenchmarkProblem`` is what a dataset
loader produces; ``RunResult`` is what a single codegen+execute
cycle produces; ``BenchmarkSummary`` is what an aggregator returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, kw_only=True)
class BenchmarkProblem:
    """A single benchmark task.

    ``problem_id`` is the dataset's natural ID (e.g. ``"mbpp/421"``).

    ``prompt`` is the LLM-facing description: natural-language task
    statement, expected signature, examples. Datasets format this
    in their own way; the loader is responsible.

    ``setup_code`` is Python code that must execute BEFORE the
    candidate code, in the same namespace. Used for SciCode-style
    subproblem dependencies where the candidate references earlier
    subproblems' outputs. Empty string when not applicable.

    ``test_code`` is the verification code. It runs in the same
    namespace as the candidate and ``setup_code``. It MUST raise
    ``AssertionError`` (or any exception) on failure and exit
    cleanly on pass. Typically a sequence of ``assert`` statements
    or a ``unittest.TestCase``.

    ``entry_point`` is the symbol the test_code expects the
    candidate to define (e.g., ``"solve"`` for the function name).
    Used when the codegen pipeline needs to know what symbol to
    produce; can be empty for free-form problems.

    ``metadata`` is dataset-specific extras (subproblem index,
    difficulty tier, etc.).
    """

    problem_id: str
    prompt: str
    setup_code: str = ""
    test_code: str
    entry_point: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RunResult:
    """Outcome of running one BenchmarkProblem through one codegen.

    ``passed`` is the boolean pass@1 verdict. ``error_class`` and
    ``error_message`` are populated on failure so we can bucket
    failure modes (compile-time vs assertion-fail vs timeout vs
    sandbox crash). ``wall_seconds`` tracks end-to-end time
    including LLM latency + execution.

    ``generated_code`` is the candidate the codegen produced —
    kept for debugging and for the "novel-test" failure-mode analysis
    after a sweep.
    """

    problem_id: str
    codegen_name: str
    passed: bool
    error_class: str | None = None
    error_message: str | None = None
    wall_seconds: float = 0.0
    generated_code: str = ""
    # G100 (2026-05-17): per-problem token usage. Populated by the
    # runner via tests.benchmarks.token_accountant. When the LLM
    # endpoint doesn't surface token counts (e.g., some Ollama
    # configs), counts stay 0 but n_llm_calls is still captured.
    # Default to 0 so historical result JSONs deserialize cleanly.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    n_llm_calls: int = 0

    @property
    def status(self) -> str:
        if self.passed:
            return "pass"
        if self.error_class == "Timeout":
            return "timeout"
        if self.error_class == "AssertionError":
            return "fail_assertion"
        if self.error_class is None:
            return "fail_unknown"
        return "fail_other"


@dataclass(frozen=True, kw_only=True)
class BenchmarkSummary:
    """Aggregate of N RunResults — what a sweep reports.

    Holds the raw results plus the pass@1 number.
    """

    dataset_name: str
    codegen_name: str
    results: tuple[RunResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_at_1(self) -> float:
        if not self.results:
            return 0.0
        return self.passed / self.total

    def status_histogram(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts
