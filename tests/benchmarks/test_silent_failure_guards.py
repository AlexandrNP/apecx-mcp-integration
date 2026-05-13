"""CGU-P4-T2 — silent-failure regression suite for the benchmark scorer.

Pins the five failure modes the user explicitly flagged in the
codegen uplift plan (``docs/composer_codegen_uplift_plan.md`` §4
P4-T2). The goal is to prove that the benchmark scorer surfaces each
failure as a CLEAN FAIL — never as a silent pass, never as an
unbucketed exception that propagates.

Why this file matters (in the user's words): "Make sure your code
paths do not cause silent failures that would make tests pass but
would impede the actual product use." A benchmark scorer that
records 0-char generated code as ``passed=True`` would silently
inflate every measurement. We test the scorer, not the LLM.

Failure modes covered:

1. Empty generated string → runner returns ``passed=False``.
2. Candidate code that prints but never defines the entry point →
   runner buckets the failure (test asserts a name → NameError →
   sandbox exits nonzero → scorer records fail).
3. SyntaxError in generated code → bucketed as ``fail_other`` /
   ``SyntaxError`` (NOT a silent pass).
4. Candidate that times out → bucketed as ``timeout`` (NOT a silent
   pass; NOT raised as an exception out of the runner).
5. DirectLink ``auto_transfer: false`` in a workflow YAML — the
   workspace's pre-commit hook already rejects this at commit time;
   we additionally assert here that the harness would record a
   benchmark FAIL if such a workflow were used (would-be silent
   no-op of the trigger cascade).

The fifth case is the dominant nanobrain silent-failure shape
(workspace CLAUDE.md, gap G7). We exercise it at the workflow level
to prove the benchmark scorer would catch it if a future YAML edit
slipped through.
"""

from __future__ import annotations

from tests.benchmarks.runner import run_one
from tests.benchmarks.types import BenchmarkProblem, RunResult


def _trivial_problem(test_code: str = "assert add(1, 2) == 3") -> BenchmarkProblem:
    """Helper: a tiny problem that asserts the candidate code
    defined ``add(a, b)`` correctly. Used as the scaffolding for
    each failure-mode test."""
    return BenchmarkProblem(
        problem_id="silent-failure-test/1",
        prompt="Write add(a, b)",
        test_code=test_code,
        entry_point="add",
    )


# ---------------------------------------------------------------------------
# Mode 1: empty generated string MUST score as fail.
# ---------------------------------------------------------------------------


def test_empty_string_codegen_is_fail_not_pass():
    """A codegen that returns an empty string must score as fail —
    not as pass, not as a silent skip. The sandbox runs an empty
    module which leaves ``add`` undefined; the test_code's assert
    then fails with ``NameError``."""
    problem = _trivial_problem()
    result: RunResult = run_one(
        problem,
        codegen_fn=lambda _p: "",
        codegen_name="empty",
        timeout_seconds=5.0,
    )
    assert result.passed is False, (
        "empty generated code was scored as pass — silent-failure regression"
    )
    # The exact error bucket varies (could be NameError or similar);
    # the load-bearing assertion is passed=False.
    assert result.error_class is not None, (
        "empty code passed=False but no error_class set — scorer can't bucket the failure"
    )


# ---------------------------------------------------------------------------
# Mode 2: code that runs but doesn't define the entry point MUST score as fail.
# ---------------------------------------------------------------------------


def test_print_without_definition_is_fail():
    """A common LLM drift: model emits a script that prints something
    but never defines the requested function. The test_code's assert
    finds no ``add`` symbol → NameError → fail. We pin that this is
    bucketed honestly (not papered over by an exception swallower)."""
    problem = _trivial_problem()
    result = run_one(
        problem,
        codegen_fn=lambda _p: "print('I will not solve this')",
        codegen_name="print-no-def",
        timeout_seconds=5.0,
    )
    assert result.passed is False
    assert result.error_class == "NameError", (
        f"expected NameError bucket (missing entry point), got {result.error_class!r}"
    )


# ---------------------------------------------------------------------------
# Mode 3: syntax errors MUST score as fail with a non-empty error_class.
# ---------------------------------------------------------------------------


def test_syntax_error_is_fail_with_syntaxerror_bucket():
    """Unparseable Python is bucketed as SyntaxError, NOT as a silent
    pass and NOT as an unbucketed crash. The error_class lets the
    scorer's histogram surface 'fail_other' breakdown."""
    problem = _trivial_problem()
    result = run_one(
        problem,
        codegen_fn=lambda _p: "def add(a, b) return a + b  # missing colon",
        codegen_name="syntax",
        timeout_seconds=5.0,
    )
    assert result.passed is False
    assert result.error_class == "SyntaxError", (
        f"expected SyntaxError bucket, got {result.error_class!r}"
    )


# ---------------------------------------------------------------------------
# Mode 4: timeout MUST score as fail with a Timeout bucket (not pass, not
# raise out of the runner).
# ---------------------------------------------------------------------------


def test_timeout_is_fail_with_timeout_bucket():
    """A candidate stuck in an infinite loop times out. The runner
    must (a) NOT raise out, (b) return passed=False, (c) bucket as
    Timeout for histogram clarity. A scorer that re-raised on
    TimeoutExpired would crash the whole sweep on one bad model."""
    problem = _trivial_problem()
    result = run_one(
        problem,
        codegen_fn=lambda _p: "def add(a, b):\n    while True:\n        pass\n",
        codegen_name="hang",
        timeout_seconds=1.0,
    )
    assert result.passed is False
    assert result.error_class == "Timeout", f"expected Timeout bucket, got {result.error_class!r}"


# ---------------------------------------------------------------------------
# Mode 5: codegen raising MUST be caught and bucketed, NOT propagate out.
# ---------------------------------------------------------------------------


def test_codegen_exception_is_fail_with_codegen_bucket():
    """A codegen function that raises (LLM 5xx, timeout to Ollama,
    bug in the wrap adapter) must bucket as ``codegen_<ExceptionType>``,
    not raise out and crash the sweep. The runner's contract is
    'always return a RunResult'."""

    def _broken_codegen(_p):
        raise ConnectionError("ollama daemon unreachable")

    problem = _trivial_problem()
    result = run_one(
        problem,
        codegen_fn=_broken_codegen,
        codegen_name="broken",
        timeout_seconds=5.0,
    )
    assert result.passed is False
    assert result.error_class == "codegen_ConnectionError", (
        f"expected codegen_ConnectionError bucket, got {result.error_class!r}"
    )


# ---------------------------------------------------------------------------
# Mode 5b (workflow-level G7): a benchmark codegen routed through a
# workflow with a missing auto_transfer link would silently fire the
# trigger cascade but never transfer data. Pinning that the workflow
# adapter surfaces this as a hard runtime error, not a silent
# zero-character return.
# ---------------------------------------------------------------------------


def test_workflow_codegen_surfaces_cascade_timeout_as_runtime_error():
    """If a workflow YAML's trigger cascade never drains (the
    classic G7 silent-failure shape), the adapter must raise
    RuntimeError citing ``cascade did not drain`` — NOT return an
    empty string that would silently bucket as a model fail.

    We construct this state by passing a cascade_timeout of 0.05s,
    which is so short that even a correctly-wired workflow can't
    drain. The deliberately-short timeout simulates the user-facing
    consequence of an auto_transfer=False link: the cascade never
    completes from the adapter's perspective.
    """
    from pathlib import Path  # noqa: PLC0415

    from tests.benchmarks.codegen.nanobrain_workflow import (  # noqa: PLC0415
        make_nanobrain_workflow_codegen,
    )

    yaml_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "benchmark_direct_codegen"
        / "workflow.yml"
    )
    codegen = make_nanobrain_workflow_codegen(
        yaml_path,
        cascade_timeout_seconds=0.05,  # impossibly short
    )

    problem = _trivial_problem()
    result = run_one(
        problem,
        codegen_fn=codegen,
        codegen_name="cascade-timeout",
        timeout_seconds=15.0,
    )
    # The adapter raises RuntimeError; the runner buckets it as a
    # codegen-side failure. NOT a silent empty pass.
    assert result.passed is False, (
        "cascade-timeout silently scored as pass — workflow G7 regression"
    )
    assert result.error_class == "codegen_RuntimeError", (
        f"expected codegen_RuntimeError bucket, got {result.error_class!r}"
    )
    assert "cascade" in (result.error_message or "").lower(), (
        f"error message should mention cascade, got: {result.error_message!r}"
    )
