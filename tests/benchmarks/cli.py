"""Benchmark sweep CLI.

Usage:

    python -m tests.benchmarks.cli mbpp \\
        --codegen direct \\
        --model mistral-nemo:latest \\
        --limit 20 \\
        --output /tmp/mbpp_baseline.json

Designed to be re-run idempotently. JSON output captures every
result (passed, error_class, wall_seconds, generated_code) so we
can re-aggregate offline without re-running the LLM.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path

from tests.benchmarks.codegen.direct import make_direct_codegen
from tests.benchmarks.codegen.plan_then_code import make_plan_then_code_codegen
from tests.benchmarks.datasets.mbpp import load_mbpp
from tests.benchmarks.runner import run_one
from tests.benchmarks.scorer import format_summary, summarize
from tests.benchmarks.types import BenchmarkProblem, RunResult


def _load_dataset(name: str, limit: int | None) -> Iterable[BenchmarkProblem]:
    if name == "mbpp":
        return load_mbpp(split="test", limit=limit)
    raise SystemExit(f"unknown dataset: {name!r}")


def _build_codegen(name: str, model: str | None, base_url: str | None):
    if name == "direct":
        return make_direct_codegen(model=model, base_url=base_url)
    if name == "plan_then_code":
        # ``--model`` controls the drafter; planner is resolved from
        # APECX_LLM_MODEL_PLANNER env or defaults to nemotron-3-nano:4b.
        return make_plan_then_code_codegen(drafter_model=model, base_url=base_url)
    raise SystemExit(f"unknown codegen: {name!r}")


def _results_to_json(results: list[RunResult]) -> list[dict]:
    return [
        {
            "problem_id": r.problem_id,
            "codegen_name": r.codegen_name,
            "passed": r.passed,
            "error_class": r.error_class,
            "error_message": r.error_message,
            "wall_seconds": r.wall_seconds,
            "generated_code": r.generated_code,
        }
        for r in results
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", choices=["mbpp"])
    parser.add_argument(
        "--codegen",
        default="direct",
        choices=["direct", "plan_then_code"],
        help="codegen strategy",
    )
    parser.add_argument("--model", default=None, help="LLM model name")
    parser.add_argument("--base-url", default=None, help="LLM endpoint base URL")
    parser.add_argument("--limit", type=int, default=20, help="cap on problems (None for all)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="per-problem wall-clock cap in seconds",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional path to dump JSON results",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-problem progress lines",
    )
    args = parser.parse_args()

    problems = list(_load_dataset(args.dataset, args.limit))
    codegen = _build_codegen(args.codegen, args.model, args.base_url)

    print(
        f"Sweeping {len(problems)} problems "
        f"[dataset={args.dataset} codegen={args.codegen} model={args.model or 'default'}]"
    )
    sweep_started = time.monotonic()
    results: list[RunResult] = []
    for i, problem in enumerate(problems, 1):
        result = run_one(
            problem,
            codegen,
            codegen_name=args.codegen,
            timeout_seconds=args.timeout,
        )
        results.append(result)
        if not args.quiet:
            tag = "PASS" if result.passed else f"FAIL[{result.error_class}]"
            print(
                f"  [{i:3d}/{len(problems)}] {problem.problem_id:<20s} "
                f"{tag} ({result.wall_seconds:.1f}s)"
            )

    elapsed = time.monotonic() - sweep_started
    summary = summarize(results, dataset_name=args.dataset, codegen_name=args.codegen)
    print()
    print(format_summary(summary))
    print(f"\nTotal wall time: {elapsed:.1f}s ({elapsed / max(len(results), 1):.1f}s/problem)")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "dataset": args.dataset,
            "codegen": args.codegen,
            "model": args.model,
            "limit": args.limit,
            "pass_at_1": summary.pass_at_1,
            "passed": summary.passed,
            "total": summary.total,
            "status_histogram": summary.status_histogram(),
            "elapsed_seconds": elapsed,
            "results": _results_to_json(results),
        }
        args.output.write_text(json.dumps(payload, indent=2))
        print(f"\nResults written to {args.output}")

    return 0 if summary.passed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
