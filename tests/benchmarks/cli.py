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
from tests.benchmarks.codegen.nanobrain_workflow import make_nanobrain_workflow_codegen
from tests.benchmarks.codegen.plan_then_code import make_plan_then_code_codegen
from tests.benchmarks.datasets.mbpp import load_mbpp
from tests.benchmarks.datasets.nanobrain_native import load_nanobrain_native
from tests.benchmarks.datasets.scicode import load_scicode
from tests.benchmarks.exclusions import (
    load_blocklist_from_results,
    merge_exclusions,
)
from tests.benchmarks.runner import run_one
from tests.benchmarks.scorer import format_summary, summarize
from tests.benchmarks.types import BenchmarkProblem, RunResult


def _load_dataset(
    name: str,
    limit: int | None,
    exclude: set[str] | None = None,
    *,
    split: str | None = None,
) -> Iterable[BenchmarkProblem]:
    if name == "mbpp":
        return load_mbpp(split=split or "test", limit=limit, exclude=exclude)
    if name == "scicode":
        # Default split is 'validation' — the only one usable without
        # the gated test_data.h5 artifact. Operators with the file
        # pass --split test plus SCICODE_TEST_DATA_H5_PATH.
        return load_scicode(split=split or "validation", limit=limit, exclude=exclude)
    if name == "nanobrain_native":
        # CGU-P1-T5: hand-crafted problems exercising framework
        # competencies. Doesn't use --split — there is only one set.
        return load_nanobrain_native(limit=limit, exclude=exclude)
    raise SystemExit(f"unknown dataset: {name!r}")


def _build_codegen(name: str, model: str | None, base_url: str | None):
    if name == "direct":
        return make_direct_codegen(model=model, base_url=base_url)
    if name == "plan_then_code":
        # ``--model`` controls the drafter; planner is resolved from
        # APECX_LLM_MODEL_PLANNER env or defaults to nemotron-3-nano:4b.
        return make_plan_then_code_codegen(drafter_model=model, base_url=base_url)
    if name == "nanobrain_direct":
        # CGU-P1-T6: nanobrain-workflow-wrapped direct codegen.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_direct_codegen"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(yaml_path)
    if name == "nanobrain_direct_with_rules":
        # E3 — wrapped direct + nanobrain_rules.md condensate in the
        # drafter's system prompt. Same model, same wiring, +4.2 KB
        # of framework guidance.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_direct_with_rules"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(yaml_path)
    if name == "nanobrain_runtime_gated_review_revise":
        # NN-1: hybrid scaffold with RUNTIME compliance probe (no
        # LLM in the validator). Catches runtime failure shapes
        # (from_config exceptions, RuntimeError, ImportError) that
        # the AST validator misses.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_runtime_gated_review_revise"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="drafter_input",
            code_source_du_name="workflow_output",
            read_from_workflow_output=True,
            cascade_timeout_seconds=180.0,
        )
    if name == "nanobrain_ast_gated_review_revise":
        # CGU-P2-T1b — hybrid LLM + AST-deterministic scaffold.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_ast_gated_review_revise"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="drafter_input",
            code_source_du_name="workflow_output",
            read_from_workflow_output=True,
        )
    if name == "nanobrain_review_revise":
        # CGU-P2-T1 — drafter -> reviewer -> reviser linear chain.
        # All three stages use nanobrain_rules.md.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_review_revise"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="drafter_input",
            code_source_step_name="reviser",
            code_source_du_name="drafter_output",
        )
    if name == "nanobrain_plan_then_code_with_rules":
        # E5 — plan-then-code with nanobrain_rules.md on the drafter.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_plan_then_code_with_rules"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="planner_input",
            code_source_step_name="drafter",
            code_source_du_name="drafter_output",
        )
    if name == "nanobrain_plan_then_code":
        # CGU-P1-T6 second wrap: planner (nemotron-3-nano:4b) ->
        # drafter (mistral-nemo:latest). Two-stage scaffold via
        # nanobrain DirectLink chain. The first step's input DU is
        # ``planner_input``; the final code lives in
        # ``drafter.drafter_output``.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_plan_then_code"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="planner_input",
            code_source_step_name="drafter",
            code_source_du_name="drafter_output",
        )
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
    parser.add_argument("dataset", choices=["mbpp", "scicode", "nanobrain_native"])
    parser.add_argument(
        "--split",
        default=None,
        help=(
            "Dataset split override (mbpp default: test, scicode default: validation). "
            "SciCode test split requires SCICODE_TEST_DATA_H5_PATH."
        ),
    )
    parser.add_argument(
        "--codegen",
        default="direct",
        choices=[
            "direct",
            "plan_then_code",
            "nanobrain_direct",
            "nanobrain_direct_with_rules",
            "nanobrain_plan_then_code",
            "nanobrain_plan_then_code_with_rules",
            "nanobrain_review_revise",
            "nanobrain_ast_gated_review_revise",
            "nanobrain_runtime_gated_review_revise",
        ],
        help=(
            "codegen strategy. ``direct`` / ``plan_then_code`` are "
            "procedural; ``nanobrain_direct`` is the framework-native "
            "Workflow.from_config wrap of direct (CGU-P1-T6)."
        ),
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
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help=(
            "Problem ID to exclude. Repeatable. Skips in addition to "
            "the dataset's hardcoded blocklist."
        ),
    )
    parser.add_argument(
        "--exclude-from",
        type=Path,
        default=None,
        help=(
            "Path to a prior sweep's JSON output. Problems that failed "
            "with Timeout or codegen_* (LLM-side) errors will be skipped. "
            "AssertionError failures are NOT excluded (they're cleanly-failed)."
        ),
    )
    args = parser.parse_args()

    derived_exclude: set[str] = set()
    if args.exclude_from is not None:
        derived_exclude = load_blocklist_from_results(args.exclude_from)
        if derived_exclude:
            print(f"Excluding {len(derived_exclude)} problems from {args.exclude_from}")
    exclude_set = merge_exclusions(set(args.exclude), derived_exclude)

    problems = list(_load_dataset(args.dataset, args.limit, exclude=exclude_set, split=args.split))
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
