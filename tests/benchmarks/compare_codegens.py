"""Cross-codegen comparison + Pareto analysis for benchmark sweeps.

Given a set of result JSONs from ``cli.py`` runs across (dataset,
codegen) pairs, produce a single comparison table + per-problem
diff highlights. Used to consolidate the G96 large-N sweep results
into the docs.

Usage:

    python -m tests.benchmarks.compare_codegens \\
        --result /tmp/bench_mbpp_direct_n100.json \\
        --result /tmp/bench_mbpp_tdr_n100.json \\
        --result /tmp/bench_mbpp_hd_rss_n100.json \\
        --output /tmp/comparison_mbpp.md

Outputs:
* Pass@1 + wall-time-per-problem table
* Per-problem diff: which codegens fixed which problems
* Cost-efficiency: pass-per-second metric
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _summarize(data: dict) -> dict:
    results = data.get("results", [])
    n = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    wall = sum(r["wall_seconds"] for r in results)
    return {
        "dataset": data.get("dataset", "?"),
        "codegen": data.get("codegen", "?"),
        "model": data.get("model", "?"),
        "n": n,
        "n_pass": n_pass,
        "pass_at_1": n_pass / n if n else 0,
        "wall_total": wall,
        "wall_per_problem": wall / n if n else 0,
        "results_by_id": {r["problem_id"]: r for r in results},
    }


def _per_problem_table(summaries: list[dict]) -> str:
    """One row per problem; one column per codegen."""
    codegens = [s["codegen"] for s in summaries]
    all_ids = sorted({pid for s in summaries for pid in s["results_by_id"]})

    lines: list[str] = []
    lines.append("| Problem | " + " | ".join(codegens) + " |")
    lines.append("|---|" + "|".join("---" for _ in codegens) + "|")
    for pid in all_ids:
        cells = []
        for s in summaries:
            r = s["results_by_id"].get(pid)
            if r is None:
                cells.append("—")
            elif r["passed"]:
                cells.append("✅")
            else:
                cells.append(f"❌ {r.get('error_class', '?')[:20]}")
        lines.append(f"| {pid} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _diff_table(summaries: list[dict]) -> str:
    """Identify problems where codegens diverged: at least one passes,
    at least one fails. Sorted by divergence severity."""
    all_ids = sorted({pid for s in summaries for pid in s["results_by_id"]})
    divergent = []
    for pid in all_ids:
        pass_set = {
            s["codegen"]: s["results_by_id"][pid]["passed"]
            for s in summaries
            if pid in s["results_by_id"]
        }
        if len(set(pass_set.values())) > 1:  # at least one True + one False
            divergent.append((pid, pass_set))

    if not divergent:
        return "(No divergences — every codegen agrees on every problem.)"

    lines: list[str] = []
    codegens = [s["codegen"] for s in summaries]
    lines.append("| Problem | " + " | ".join(codegens) + " | Pattern |")
    lines.append("|---|" + "|".join("---" for _ in codegens) + "|---|")
    for pid, pass_set in divergent:
        cells = ["✅" if pass_set.get(cg, False) else "❌" for cg in codegens]
        # Tag the pattern: which codegens fixed it
        fixers = [cg for cg, p in pass_set.items() if p]
        breakers = [cg for cg, p in pass_set.items() if not p]
        tag = f"{','.join(fixers)} fix; {','.join(breakers)} miss"
        lines.append(f"| {pid} | " + " | ".join(cells) + f" | {tag} |")
    return "\n".join(lines)


def _summary_table(summaries: list[dict]) -> str:
    lines: list[str] = []
    lines.append(
        "| Codegen | n | Pass | Pass@1 | Wall (s) | Wall/problem (s) | Cost mult vs direct |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    direct = next((s for s in summaries if s["codegen"] == "direct"), summaries[0])
    direct_wall = direct["wall_per_problem"] or 1
    for s in summaries:
        cost_mult = s["wall_per_problem"] / direct_wall
        lines.append(
            f"| {s['codegen']} | {s['n']} | {s['n_pass']} | "
            f"{s['pass_at_1']:.3f} | {s['wall_total']:.1f} | "
            f"{s['wall_per_problem']:.1f} | {cost_mult:.2f}× |"
        )
    return "\n".join(lines)


def render_comparison(result_paths: list[Path]) -> str:
    summaries = [_summarize(_load(p)) for p in result_paths]
    if not summaries:
        return "(No results.)"

    # Group by dataset
    by_dataset = defaultdict(list)
    for s in summaries:
        by_dataset[s["dataset"]].append(s)

    out: list[str] = []
    out.append("# Cross-codegen benchmark comparison\n")
    for dataset, group in by_dataset.items():
        out.append(f"\n## Dataset: {dataset}\n")
        out.append(f"**Model**: {group[0]['model']}, **N**: {group[0]['n']}\n")
        out.append("\n### Summary\n")
        out.append(_summary_table(group))
        out.append("\n### Per-problem divergences\n")
        out.append(_diff_table(group))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-codegen comparison + Pareto analysis.")
    parser.add_argument(
        "--result",
        action="append",
        type=Path,
        required=True,
        help="Path to a benchmark result JSON. Repeat for each (dataset,codegen) pair.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Markdown file to write. Stdout if omitted.",
    )
    args = parser.parse_args(argv)

    rendered = render_comparison(args.result)
    if args.output:
        args.output.write_text(rendered)
        print(f"Wrote comparison to {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
