"""Aggregate RunResults into pass@1 summaries.

Tiny by design — anything fancier (pass@k, weighted scoring,
difficulty-bucketed pass rates) belongs in a separate module so
the core stays auditable.
"""

from __future__ import annotations

from collections.abc import Iterable

from tests.benchmarks.types import BenchmarkSummary, RunResult


def summarize(
    results: Iterable[RunResult],
    *,
    dataset_name: str,
    codegen_name: str,
) -> BenchmarkSummary:
    """Roll up a stream of RunResults into a BenchmarkSummary."""
    tuple_results = tuple(results)
    return BenchmarkSummary(
        dataset_name=dataset_name,
        codegen_name=codegen_name,
        results=tuple_results,
    )


def format_summary(summary: BenchmarkSummary, *, show_failures: int = 10) -> str:
    """Human-readable summary block.

    ``show_failures`` is the cap on how many per-failure lines we
    dump. A sweep over 500 MBPP problems shouldn't bury the terminal
    in stack traces.
    """
    lines = [
        f"=== Benchmark: {summary.dataset_name} | Codegen: {summary.codegen_name} ===",
        f"Pass@1: {summary.pass_at_1:.3f}  ({summary.passed}/{summary.total})",
    ]
    hist = summary.status_histogram()
    if hist:
        lines.append("Status histogram:")
        for status, count in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {status}: {count}")

    failures = [r for r in summary.results if not r.passed]
    if failures and show_failures > 0:
        lines.append(f"First {min(show_failures, len(failures))} failures:")
        for r in failures[:show_failures]:
            msg = (r.error_message or "").splitlines()
            tail = msg[-1] if msg else ""
            lines.append(f"  - {r.problem_id} [{r.error_class}] {tail[:80]}")

    return "\n".join(lines)


__all__ = ["summarize", "format_summary"]
