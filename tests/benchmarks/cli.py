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
from tests.benchmarks.codegen.rhea_workflow import make_rhea_workflow_codegen
from tests.benchmarks.datasets.biomlbench import load_biomlbench
from tests.benchmarks.datasets.bioprobench import load_bioprobench
from tests.benchmarks.datasets.bixbench import load_bixbench
from tests.benchmarks.datasets.mbpp import load_mbpp
from tests.benchmarks.datasets.nanobrain_native import load_nanobrain_native
from tests.benchmarks.datasets.open_rosalind import load_open_rosalind
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
    if name == "open_rosalind":
        # Codegen-adapted subset of Open-Rosalind's sequence_basic
        # category. Open-Rosalind has NO train/val/test — the splits
        # are release files: v0 (canonical, 32 tasks), v1 (49),
        # holdout (30). Default split = v0.
        return load_open_rosalind(split=split or "v0", limit=limit, exclude=exclude)
    if name == "bixbench":
        # BixBench codegen-adapted subset. Canonical split is the HF
        # dataset's single eval split (BixBench is eval-only). The
        # eval_modes filter restricts to the Python-answerable subset
        # (str_verifier + range_verifier = 122 questions); llm_verifier
        # questions need an LLM judge not wired into the subprocess
        # sandbox. See the loader docstring for the adaptation rationale.
        return load_bixbench(
            split=split or "eval",
            limit=limit,
            exclude=exclude,
            eval_modes={"str_verifier", "range_verifier"},
        )
    if name == "biomlbench":
        # CONFIGURED but run-deferred per user instruction. The loader
        # FAILS LOUDLY if invoked without data — it does not skip.
        return load_biomlbench(split=split, limit=limit, exclude=exclude)
    if name == "bioprobench":
        # CONFIGURED but run-deferred per user instruction. The loader
        # FAILS LOUDLY if invoked without data — it does not skip.
        return load_bioprobench(split=split, limit=limit, exclude=exclude)
    raise SystemExit(f"unknown dataset: {name!r}")


def _build_codegen(name: str, model: str | None, base_url: str | None):
    if name == "direct":
        return make_direct_codegen(model=model, base_url=base_url)
    if name == "rhea_workflow":
        # The code generator uses Rhea as an MCP server: discovers the
        # Rhea tool catalog at factory time, then GENERATES a workflow
        # per problem that dispatches the matching tool. FAILS LOUD at
        # factory time if $RHEA_MCP_URL is unset. Intended for the
        # open_rosalind dataset (the standalone OR-via-Rhea case).
        return make_rhea_workflow_codegen()
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
    if name == "nanobrain_retrieval_grounded_mbpp":
        # F17 extended: router with MBPP sub-categories (string /
        # list / math / default).
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_retrieval_grounded_mbpp"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="drafter",
            code_source_du_name="drafter_output",
            cascade_timeout_seconds=120.0,
        )
    # Ablation codegens: isolate which component(s) drive the +10pp
    # nanobrain-native lift of integrated_similarity. Each ablation
    # differs from F17 retrieval_grounded by exactly one or two added
    # nodes (memory_reader, aggregator, memory_recorder).
    _ablation_specs = {
        "nanobrain_ablation_memreader_only": (
            "benchmark_ablation_memreader_only",
            "drafter",
            "drafter_output",
        ),
        "nanobrain_ablation_aggregator_only": (
            "benchmark_ablation_aggregator_only",
            "aggregator",
            "aggregator_output",
        ),
        "nanobrain_ablation_memrecorder_only": (
            "benchmark_ablation_memrecorder_only",
            "drafter",
            "drafter_output",
        ),
        "nanobrain_ablation_memreader_aggregator": (
            "benchmark_ablation_memreader_aggregator",
            "aggregator",
            "aggregator_output",
        ),
        "nanobrain_ablation_aggregator_memrecorder": (
            "benchmark_ablation_aggregator_memrecorder",
            "aggregator",
            "aggregator_output",
        ),
    }
    if name in _ablation_specs:
        from pathlib import Path  # noqa: PLC0415

        wf_dir, src_step, src_du = _ablation_specs[name]
        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / wf_dir
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name=src_step,
            code_source_du_name=src_du,
            cascade_timeout_seconds=120.0,
        )
    if name == "nanobrain_max_power":
        # Kitchen-sink composition: router + similarity-memory +
        # perturbing drafter + AST voter + AST-gated recorder.
        # The expected-best scaffold per the user's "max power" framing.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_max_power"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="aggregator",
            code_source_du_name="aggregator_output",
            cascade_timeout_seconds=300.0,
        )
    if name == "nanobrain_max_power_websearch":
        # max_power + a WebSearchContextStep node between memory_reader
        # and the drafter. Ablation pair: this vs nanobrain_max_power.
        # The web_search_context node is non-deterministic (live web);
        # its tool config sets a cache_dir for reproducible re-runs.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_max_power_websearch"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="aggregator",
            code_source_du_name="aggregator_output",
            # +1 node + per-problem network round-trip vs max_power.
            cascade_timeout_seconds=360.0,
        )
    if name == "nanobrain_ablation_websearch_only":
        # F17 retrieval_grounded + ONE WebSearchContextStep node.
        # Ablation pair: this vs nanobrain_retrieval_grounded. Isolates
        # the marginal effect of web search on the F17 baseline.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_ablation_websearch_only"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="drafter",
            code_source_du_name="drafter_output",
            # 3-node cascade + per-problem network round-trip.
            cascade_timeout_seconds=180.0,
        )
    if name == "nanobrain_integrated_similarity":
        # Item 3 (MemFlow tier-2): F17 winner shape + cross-category
        # similarity_read mode + AST-gated memory_recorder. Isolates
        # the memory variable for diagnostic measurement.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_integrated_similarity"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="aggregator",
            code_source_du_name="aggregator_output",
            cascade_timeout_seconds=240.0,
        )
    if name == "nanobrain_perturbed_consensus":
        # Item 2 (strong-form SGDe): N samples each with a different
        # stem-phrase perturbation at temperature=0. The variance is
        # in the prompt, not the sampler. F18 showed the weak form
        # (temperature-variance) regresses by -10pp on nanobrain-native;
        # this is the SGDe-paper-aligned strong form.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_perturbed_consensus"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="aggregator",
            code_source_du_name="aggregator_output",
            cascade_timeout_seconds=240.0,
        )
    if name == "nanobrain_integrated_full":
        # All three post-F17 components composed: router (worked
        # examples) -> memory_reader -> multi_drafter (N=3, T=0.5)
        # -> aggregator (AST voter) -> memory_recorder. Predicted to
        # under-perform F17 on pass@1 (F18: multi_drafter regresses);
        # useful for cross-run memory build-up + trigger-cascade
        # stress + adoption demo.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_integrated_full"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="aggregator",
            code_source_du_name="aggregator_output",
            cascade_timeout_seconds=300.0,  # 6-node cascade w/ N=3 fan-out
        )
    if name == "nanobrain_structural_consensus":
        # SGDe-style fan-out/fan-in scaffold: N samples at temp > 0
        # -> deterministic AST voter.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_structural_consensus"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="aggregator",
            code_source_du_name="aggregator_output",
            cascade_timeout_seconds=240.0,  # N=3 samples in parallel; ~30-60s typical
        )
    if name == "nanobrain_retrieval_grounded":
        # Retrieval-grounded + per-task-class scaffold for nanobrain-
        # native. Deterministic classifier + per-category worked
        # example -> drafter. Targets F15's "per-task-class curated
        # prompts" direction-change.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_retrieval_grounded"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="router_input",
            code_source_step_name="drafter",
            code_source_du_name="drafter_output",
            cascade_timeout_seconds=120.0,
        )
    if name == "nanobrain_edge_case_then_code":
        # MB-1 from composer_scaffold_designs_per_benchmark.md.
        # Edge-case enumerator (small LLM) -> drafter (large LLM).
        # Tuned for MBPP-class algorithmic problems.
        from pathlib import Path  # noqa: PLC0415

        yaml_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "benchmark_edge_case_then_code"
            / "workflow.yml"
        )
        return make_nanobrain_workflow_codegen(
            yaml_path,
            first_step_input_du_name="edge_case_input",
            code_source_step_name="drafter",
            code_source_du_name="drafter_output",
            cascade_timeout_seconds=120.0,
        )
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
    parser.add_argument(
        "dataset",
        choices=[
            "mbpp",
            "scicode",
            "nanobrain_native",
            "open_rosalind",
            "bixbench",
            "biomlbench",
            "bioprobench",
        ],
    )
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
            "nanobrain_edge_case_then_code",
            "nanobrain_retrieval_grounded",
            "nanobrain_retrieval_grounded_mbpp",
            "nanobrain_structural_consensus",
            "nanobrain_perturbed_consensus",
            "nanobrain_integrated_full",
            "nanobrain_integrated_similarity",
            "nanobrain_max_power",
            "nanobrain_max_power_websearch",
            "nanobrain_ablation_websearch_only",
            "nanobrain_ablation_memreader_only",
            "nanobrain_ablation_aggregator_only",
            "nanobrain_ablation_memrecorder_only",
            "nanobrain_ablation_memreader_aggregator",
            "nanobrain_ablation_aggregator_memrecorder",
            "rhea_workflow",
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
