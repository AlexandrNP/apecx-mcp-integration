"""Self-refining controller for the viral_epitope_analysis eval. Mirrors the harmonization/workflow
refine-loop shape: evaluate → diagnose (fixed taxonomy, auto_safe/gated gates) → refine (auto_safe:
re-run transient + expand virus set; gated: evidence worklist) → terminate (cap/converge/plateau).

Two epitope-specific things vs the generic loop:
  • the checks are reason-aware (an honest degrade is NOT a bug — see epitope_checks), so a passing run
    can still have empty artifacts;
  • a CROSS-VIRUS verdict: ProtaBank is "reported" on every run, but if it is 0 for ALL tested viruses it
    is reported-but-useless — a real reliability gap (`protabank_never_retrieved`).

The pure logic (diagnose, protabank_verdict, split_held_out, should_terminate) is unit-tested without
infra; run_loop drives the live run_epitope.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from tests.eval.run_epitope import EpitopeResult, run_epitope

_OUT = Path(__file__).resolve().parent / "output"

_CATEGORY_GATE = {
    "transient": "auto_safe",
    "rhea_unavailable": "informational",
    "protabank_zero_but_biased_sample": "informational",
    "no_streaming": "gated",
    "incomplete": "gated",
    "silent_empty_artifact": "gated",
    "missing_references": "gated",
    "protabank_silently_omitted": "gated",
    "protabank_never_retrieved": "gated",
    "step_failed": "gated",
}
_CHECK_TO_CATEGORY = {
    "streaming": "no_streaming",
    "completeness": "incomplete",
    "full_artifacts": "silent_empty_artifact",
    "report_references": "missing_references",
    "protabank_reported": "protabank_silently_omitted",
    "structural_reasoning_produced": "structural_reasoning_empty",
}


@dataclass
class FailureItem:
    query: str
    category: str
    gate: str
    evidence: str


def diagnose_run(r: EpitopeResult) -> list[FailureItem]:
    """Per-run failures → the fixed taxonomy. A reason-aware-passing run yields none."""
    if r.passed:
        return []
    if r.transient:
        return [FailureItem(r.query, "transient", "auto_safe", (r.error or "")[:200])]
    if r.error and r.error.startswith("rhea_unavailable"):
        # the protein/sequence leg requires RHEA, which is down — environment, not a code bug
        return [FailureItem(r.query, "rhea_unavailable", "informational", r.error[:200])]
    items = [
        FailureItem(
            r.query,
            _CHECK_TO_CATEGORY.get(c.name, "step_failed"),
            _CATEGORY_GATE.get(_CHECK_TO_CATEGORY.get(c.name, "step_failed"), "gated"),
            c.evidence,
        )
        for c in r.checks
        if not c.passed
    ]
    if r.error and not items:
        items.append(FailureItem(r.query, "step_failed", "gated", r.error[:200]))
    return items


def protabank_verdict(results: list[EpitopeResult]) -> FailureItem | None:
    """CROSS-VIRUS verdict on ProtaBank — SAMPLE-AWARE (lesson EF1): a run that didn't complete reports
    protabank=None and is EXCLUDED from the count. The data-RICH viruses (heavily-published: SARS-CoV-2 /
    influenza / HIV) are exactly the ones that tend to fail/halt — so "0 on all completing viruses" over a
    sample that DROPPED them is a FALSE 'never retrieved'. Three outcomes:
      • any completing virus retrieved >0  → None (ProtaBank works).
      • all-0 AND some runs excluded       → informational (biased sample, not a reliable verdict).
      • all-0 AND none excluded            → gated (genuinely never surfaces data).
    """
    counts = [r.protabank for r in results if r.protabank is not None]
    excluded = [r.query for r in results if r.protabank is None]
    if not counts or any(n > 0 for n in counts):
        return None
    if excluded:
        return FailureItem(
            "<completing viruses>",
            "protabank_zero_but_biased_sample",
            "informational",
            f"ProtaBank 0 on all {len(counts)} COMPLETING viruses, but {len(excluded)} excluded "
            f"(incomplete: {excluded[:3]}) — the data-rich heavy viruses may be among them; not a "
            "reliable 'never retrieved' verdict until they complete.",
        )
    return FailureItem(
        "<all tested viruses>",
        "protabank_never_retrieved",
        "gated",
        f"ProtaBank 0 on all {len(counts)} viruses (none excluded) — genuinely never surfaces data.",
    )


def diagnose(results: list[EpitopeResult]) -> list[FailureItem]:
    items: list[FailureItem] = []
    for r in results:
        items.extend(diagnose_run(r))
    v = protabank_verdict(results)
    if v is not None:
        items.append(v)
    return items


def split_held_out(questions, every: int = 4):
    ordered = sorted(set(questions))
    held = ordered[::every] if every > 0 else []
    train = [q for q in ordered if q not in set(held)]
    return train, held


def should_terminate(history: list[dict], max_iters: int):
    if len(history) >= max_iters:
        return True, "max_iters"
    if not history:
        return False, ""
    last = history[-1]
    if last.get("held_all_pass") and last.get("auto_safe_pending", 0) == 0:
        return True, "converged"
    if len(history) >= 2 and set(last.get("categories", [])) == set(
        history[-2].get("categories", [])
    ):
        return True, "plateau"
    return False, ""


def _parse_question(line: str) -> tuple[str, str | None]:
    """`virus|protein` → (virus, protein); a bare `virus` → (virus, None)."""
    if "|" in line:
        virus, protein = line.split("|", 1)
        return virus.strip(), (protein.strip() or None)
    return line.strip(), None


def run_loop(
    questions: list[str], *, max_iters: int = 2, held_out_every: int = 4, out_dir: Path = _OUT
):
    out_dir.mkdir(parents=True, exist_ok=True)
    train, held = split_held_out(questions, held_out_every)
    history: list[dict] = []
    worklist: list[FailureItem] = []
    for _ in range(max_iters):
        train_r = [run_epitope(*_parse_question(q)) for q in train]
        held_r = [run_epitope(*_parse_question(q)) for q in held]
        failures = diagnose(train_r + held_r)
        worklist = _dedupe(worklist + [f for f in failures if f.gate == "gated"])
        history.append(
            {
                "train": len(train),
                "held": len(held),
                "passed": sum(r.passed for r in train_r),
                "held_all_pass": all(r.passed for r in held_r) if held_r else False,
                "auto_safe_pending": sum(1 for f in failures if f.gate == "auto_safe"),
                "protabank_counts": [r.protabank for r in train_r + held_r],
                "categories": sorted({f.category for f in failures}),
            }
        )
        stop, reason = should_terminate(history, max_iters)
        if stop:
            history[-1]["stop_reason"] = reason
            break
    _write(out_dir / "epitope_refine_history.json", history)
    _write(out_dir / "epitope_worklist.json", [f.__dict__ for f in worklist])
    return {"history": history, "worklist": [f.__dict__ for f in worklist]}


def _dedupe(items):
    seen, out = set(), []
    for f in items:
        k = (f.query, f.category)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def _write(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-iters", type=int, default=2)
    ap.add_argument("--held-out-every", type=int, default=4)
    ap.add_argument(
        "--questions", type=Path, default=Path(__file__).resolve().parent / "epitope_questions.txt"
    )
    args = ap.parse_args(argv)
    qs = [
        ln.strip()
        for ln in args.questions.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    summary = run_loop(qs, max_iters=args.max_iters, held_out_every=args.held_out_every)
    print(json.dumps(summary["history"], indent=2))
    print(f"\nGATED worklist: {len(summary['worklist'])} item(s) -> output/epitope_worklist.json")
    for f in summary["worklist"]:
        print(f"  [{f['category']}] {f['query'][:40]}: {f['evidence'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
