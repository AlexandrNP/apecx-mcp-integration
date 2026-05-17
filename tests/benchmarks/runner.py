"""BenchmarkRunner: glue between a codegen function and the sandbox.

A ``codegen_fn`` takes a ``BenchmarkProblem`` and returns a
candidate code string (the LLM's answer). The runner executes the
candidate in the sandbox against the problem's test_code and
reports a ``RunResult``.

Why this isn't a class: there's nothing to inherit. The codegen
side carries all the configuration (model, scaffold, prompts);
the runner side is a pure function. Functions compose; classes
add ceremony.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from tests.benchmarks.sandbox import run_in_subprocess
from tests.benchmarks.token_accountant import count_tokens
from tests.benchmarks.types import BenchmarkProblem, RunResult

CodegenFn = Callable[[BenchmarkProblem], str]


def run_one(
    problem: BenchmarkProblem,
    codegen_fn: CodegenFn,
    *,
    codegen_name: str,
    timeout_seconds: float = 30.0,
) -> RunResult:
    """Run a single problem through a single codegen and report.

    Failure modes are bucketed:

    - The codegen raises → ``error_class="codegen_<ExceptionType>"``.
      Counts as fail; we do NOT retry. Repair loops belong in the
      codegen itself, not in the runner — keeping the runner
      stupid makes scaffolds composable.
    - Sandbox times out → ``error_class="Timeout"``, ``passed=False``.
    - Sandbox exits nonzero → ``error_class`` parsed from the
      traceback if present, else ``"NonZeroExit"``.
    - Sandbox exits 0 → ``passed=True``.

    G100 (2026-05-17): Wraps the codegen call in ``count_tokens()``
    so per-problem LLM token usage lands in the RunResult. Counts
    are best-effort — endpoints that don't surface usage metadata
    (some Ollama configs) will record n_llm_calls > 0 but tokens = 0.
    """
    started = time.monotonic()

    with count_tokens() as tokens:
        try:
            candidate_code = codegen_fn(problem)
        except BaseException as exc:  # noqa: BLE001
            return RunResult(
                problem_id=problem.problem_id,
                codegen_name=codegen_name,
                passed=False,
                error_class=f"codegen_{type(exc).__name__}",
                error_message=str(exc)[:500],
                wall_seconds=time.monotonic() - started,
                generated_code="",
                prompt_tokens=tokens.prompt_tokens,
                completion_tokens=tokens.completion_tokens,
                n_llm_calls=tokens.n_calls,
            )

    sandbox_result = run_in_subprocess(
        candidate_code=candidate_code,
        setup_code=problem.setup_code,
        test_code=problem.test_code,
        timeout_seconds=timeout_seconds,
    )

    common_token_kwargs = {
        "prompt_tokens": tokens.prompt_tokens,
        "completion_tokens": tokens.completion_tokens,
        "n_llm_calls": tokens.n_calls,
    }

    if sandbox_result.timed_out:
        return RunResult(
            problem_id=problem.problem_id,
            codegen_name=codegen_name,
            passed=False,
            error_class="Timeout",
            error_message=f"exceeded {timeout_seconds}s",
            wall_seconds=time.monotonic() - started,
            generated_code=candidate_code,
            **common_token_kwargs,
        )

    if sandbox_result.passed:
        return RunResult(
            problem_id=problem.problem_id,
            codegen_name=codegen_name,
            passed=True,
            error_class=None,
            error_message=None,
            wall_seconds=time.monotonic() - started,
            generated_code=candidate_code,
            **common_token_kwargs,
        )

    # Non-zero exit. Try to extract the exception class from the
    # traceback in stderr so we can bucket assertions vs runtime
    # errors vs syntax errors etc.
    err_class = _extract_error_class(sandbox_result.stderr)
    return RunResult(
        problem_id=problem.problem_id,
        codegen_name=codegen_name,
        passed=False,
        error_class=err_class,
        error_message=sandbox_result.stderr[-500:] if sandbox_result.stderr else None,
        wall_seconds=time.monotonic() - started,
        generated_code=candidate_code,
        **common_token_kwargs,
    )


def _extract_error_class(stderr: str) -> str:
    """Pull the last ``XError: msg`` line out of a traceback.

    The wrapper script in ``sandbox.py`` runs ``traceback.print_exc()``
    on any uncaught exception, so the final non-blank line of stderr
    is usually ``ExceptionType: message``. We split on ``:`` and
    take the type name. Falls back to ``"NonZeroExit"`` if we can't
    parse one out — that bucket catches segfaults and other oddities.
    """
    if not stderr:
        return "NonZeroExit"
    for line in reversed(stderr.splitlines()):
        line = line.strip()
        if not line or line.startswith("File ") or line.startswith("Traceback"):
            continue
        # Lines like ``AssertionError: x != y`` or ``NameError: name 'foo' is not defined``.
        # The class name is the first word before ":" or whitespace.
        head = line.split(":", 1)[0].strip()
        # Cheap heuristic: looks like a class name (CamelCase, no
        # spaces). Anything weirder, we bail and bucket as Unknown.
        if head and head[0].isupper() and " " not in head and len(head) < 60:
            return head
        break
    return "NonZeroExit"


__all__ = ["CodegenFn", "run_one"]
