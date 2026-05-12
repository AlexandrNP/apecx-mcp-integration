"""Smoke test for the benchmark harness itself.

These tests do NOT measure any LLM. They confirm the harness
plumbing (problem dataclass → codegen function → subprocess
sandbox → result extraction → aggregation) works on hand-crafted
inputs where we know the expected outcome.

If any of these fail, every downstream benchmark number is suspect.
"""

from __future__ import annotations

from tests.benchmarks.runner import run_one
from tests.benchmarks.scorer import summarize
from tests.benchmarks.types import BenchmarkProblem


def _add_problem() -> BenchmarkProblem:
    return BenchmarkProblem(
        problem_id="smoke/add",
        prompt="Define add(a, b) returning a + b.",
        test_code=("assert add(2, 3) == 5\nassert add(-1, 1) == 0\nassert add(0, 0) == 0\n"),
        entry_point="add",
    )


def test_correct_candidate_passes():
    problem = _add_problem()

    def codegen(_: BenchmarkProblem) -> str:
        return "def add(a, b):\n    return a + b\n"

    result = run_one(problem, codegen, codegen_name="hardcoded_correct")
    assert result.passed, f"unexpected failure: {result.error_class} / {result.error_message}"
    assert result.error_class is None
    assert "def add" in result.generated_code
    assert result.wall_seconds > 0


def test_assertion_failure_is_bucketed():
    """A candidate that compiles but fails the asserts should
    bucket as ``AssertionError`` so we can distinguish it from
    syntax/runtime failures in the histogram."""
    problem = _add_problem()

    def codegen(_: BenchmarkProblem) -> str:
        # Off by one — passes type-check, fails the asserts.
        return "def add(a, b):\n    return a + b + 1\n"

    result = run_one(problem, codegen, codegen_name="hardcoded_off_by_one")
    assert not result.passed
    assert result.error_class == "AssertionError"


def test_syntax_error_is_bucketed():
    problem = _add_problem()

    def codegen(_: BenchmarkProblem) -> str:
        return "def add(a, b\n    return a + b\n"  # missing close paren

    result = run_one(problem, codegen, codegen_name="hardcoded_syntax_error")
    assert not result.passed
    # SyntaxError or IndentationError — both surface as the same
    # failure class for our purposes; pin the existence of *some*
    # parseable error class rather than the exact name.
    assert result.error_class in ("SyntaxError", "IndentationError")


def test_runtime_error_is_bucketed():
    problem = _add_problem()

    def codegen(_: BenchmarkProblem) -> str:
        return "def add(a, b):\n    return a + bogus_name\n"

    result = run_one(problem, codegen, codegen_name="hardcoded_nameerror")
    assert not result.passed
    assert result.error_class == "NameError"


def test_codegen_exception_is_bucketed():
    """If the codegen function itself raises (e.g. LLM unreachable),
    we should bucket that distinctly from execution failures so
    operators can tell ``my LLM is down`` from ``my model is bad``."""
    problem = _add_problem()

    def codegen(_: BenchmarkProblem) -> str:
        raise RuntimeError("simulated LLM outage")

    result = run_one(problem, codegen, codegen_name="hardcoded_codegen_crash")
    assert not result.passed
    assert result.error_class == "codegen_RuntimeError"
    assert result.generated_code == ""


def test_timeout_is_bucketed():
    """An infinite loop must NOT hang the harness; the subprocess
    sandbox is supposed to kill it at the wall-clock cap."""
    problem = BenchmarkProblem(
        problem_id="smoke/spin",
        prompt="...",
        test_code="spin()  # never returns",
    )

    def codegen(_: BenchmarkProblem) -> str:
        return "def spin():\n    while True:\n        pass\n"

    result = run_one(
        problem,
        codegen,
        codegen_name="hardcoded_spinner",
        timeout_seconds=2.0,  # short to keep the test fast
    )
    assert not result.passed
    assert result.error_class == "Timeout"


def test_setup_code_runs_before_candidate():
    """SciCode-style: the candidate depends on a helper from
    earlier in the problem. Make sure setup_code runs first and is
    visible to both the candidate and the test."""
    problem = BenchmarkProblem(
        problem_id="smoke/setup",
        prompt="...",
        setup_code="def double(x):\n    return 2 * x\n",
        test_code="assert solve(3) == 12\nassert solve(0) == 0\n",
        entry_point="solve",
    )

    def codegen(_: BenchmarkProblem) -> str:
        return "def solve(x):\n    return double(double(x))\n"

    result = run_one(problem, codegen, codegen_name="hardcoded_setup_chain")
    assert result.passed, f"setup chain failed: {result.error_class} / {result.error_message}"


def test_summary_aggregates_results():
    problem = _add_problem()

    def correct(_: BenchmarkProblem) -> str:
        return "def add(a, b):\n    return a + b\n"

    def wrong(_: BenchmarkProblem) -> str:
        return "def add(a, b):\n    return a - b\n"

    r1 = run_one(problem, correct, codegen_name="c1")
    r2 = run_one(problem, wrong, codegen_name="c1")
    summary = summarize([r1, r2], dataset_name="smoke", codegen_name="c1")
    assert summary.total == 2
    assert summary.passed == 1
    assert summary.pass_at_1 == 0.5
    hist = summary.status_histogram()
    assert hist.get("pass") == 1
    assert hist.get("fail_assertion") == 1
