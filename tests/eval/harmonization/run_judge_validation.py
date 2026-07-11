"""Multi-model, multi-judge relevance validation — parallel, checkpointed, resumable.

Judges up to N distinct served records with a PANEL of LLM models, recording each record's Judge A /
Judge B / Combined verdict alongside every model's label, then computes — for EVERY judge (3 automated +
each model) — precision / recall / F1 / confusion per request type (`judge_stats`), plus an inter-judge
Cohen-κ matrix. There is no absolute ground truth, so precision/recall are AGREEMENT vs a proxy-gold
reference (panel-majority, leave-one-out for a model being scored; and the combined-automated anchor) —
read with the same caveat as κ.

Compute reality: on local Ollama a 24B judgment is ~5s and there is no real server-side parallelism, so
the full 6-model × 4000 pass is ~15–20 GPU-hours. Robustness makes that survivable: each model's labels
append to ``output/judge_labels.<model>.jsonl`` immediately, so a killed/rescheduled run RESUMES (skips
already-labeled records) — and models run one-fully-before-the-next (fast→slow) to avoid Ollama reloads.

Run (see MULTIJUDGE_RUNBOOK.md):
  PYTHONPATH=src:. .venv/bin/python -m tests.eval.harmonization.run_judge_validation \
    --core tests/eval/harmonization/output/harmonization_precision.json --n 4000 --workers 4
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import time
from pathlib import Path
from threading import Lock

from tests.eval.harmonization import judge_stats, llm_validate

_OUTDIR = Path(__file__).parent / "output"

# Ordered fast→slow so partial/interrupted runs favour the cheap models first. The two bio judges
# (medgemma = medical Gemma, medllama2 = medical LLaMA-2) are "bio-oriented" but not virology-specific.
# meditron:7b was TRIED and DROPPED — it echoes the system prompt via chat (0 parseable verdicts) and
# ignores instructions via completion; medgemma (Gemma-based) follows the JSON contract like gemma4.
_PANEL = [
    "nemotron-3-nano:4b",
    "gemma4:latest",
    "medgemma:latest",
    "medllama2:7b",
    "mistral-nemo:latest",
    "devstral:24b",
]


def _collect_rows(d: dict, cap: int) -> list[dict]:
    """Distinct judgeable served records (deduped by (record, query)), each carrying its stored Judge A /
    Judge B / Combined verdict + text + request-type labels — the same rows every model judges."""
    label_by_term = {
        q["term"]: (q.get("canonical_label") or q.get("resolved_term") or q["term"])
        for q in d["queries"]
    }
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in d["cells"]:
        term = c["term"]
        pathogen = label_by_term.get(term, term)
        for r in c.get("sample", []):
            text = r.get("title") or r.get("organism") or r.get("subjects")
            if not text:
                continue
            key = (str(r.get("primary_id") or text), term)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "key": "\t".join(key),
                    "pathogen": pathogen,
                    "category": c["category"],
                    "regime": c["regime"],
                    "judge_a": r.get("judge_a"),
                    "judge_b": r.get("judge_b"),
                    "verdict": r.get("verdict"),
                    "title": r.get("title") or "",
                    "organism": r.get("organism") or "",
                    "subjects": r.get("subjects") or "",
                }
            )
            if len(rows) >= cap:
                return rows
    return rows


def _labels_path(base: str, model: str) -> Path:
    return Path(f"{base}.{model.replace(':', '_').replace('/', '_')}.jsonl")


def _load_labels(path: Path) -> dict[str, bool]:
    """key → belongs (bool) for a model's checkpoint. Only successful (non-None) verdicts are stored.
    Tolerant of a truncated trailing line (a hard SIGKILL / disk-full between write and flush) — a
    malformed line is skipped so the record simply re-judges, never blocking the whole resume."""
    out: dict[str, bool] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # partial line from a hard kill — skip; the record re-judges on this run
            out[rec["key"]] = rec["llm"]
    return out


def _judge_with_model(rows: list[dict], model: str, path: Path, workers: int) -> dict[str, bool]:
    """Judge every row with ``model`` (resumable). Appends each successful label; None (error/unparse)
    is skipped so it retries on the next run."""
    done = _load_labels(path)
    todo = [r for r in rows if r["key"] not in done]
    print(f"=== {model}: {len(rows)} rows, {len(done)} done, {len(todo)} to judge ===", flush=True)
    if not todo:
        return done
    lock = Lock()
    t0 = time.time()
    n = [0]
    with open(path, "a") as fh:

        def work(r: dict) -> None:
            v = llm_validate.llm_judge(
                r["title"], r["organism"], r["subjects"], r["pathogen"], model=model, timeout=180
            )
            if v["belongs"] is None:
                return  # error/unparse — not checkpointed, retries on resume
            with lock:
                fh.write(json.dumps({"key": r["key"], "llm": v["belongs"]}) + "\n")
                fh.flush()
                n[0] += 1
                if n[0] % 100 == 0:
                    print(f"  [{round(time.time() - t0)}s] {model}: {n[0]}/{len(todo)}", flush=True)

        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))
    return _load_labels(path)


def run(core_path: str, cap: int, workers: int, panel: list[str], labels_base: str) -> dict:
    d = json.loads(Path(core_path).read_text())
    rows = _collect_rows(d, cap)
    per_model = {
        m: _judge_with_model(rows, m, _labels_path(labels_base, m), workers) for m in panel
    }

    # Assemble one row per record carrying every judge's call; keep only records with ≥1 model verdict.
    records: list[dict] = []
    for r in rows:
        rec = {
            "category": r["category"],
            "regime": r["regime"],
            "judge_a": r["judge_a"],
            "judge_b": r["judge_b"],
            "verdict": r["verdict"],
        }
        for m in panel:
            rec[f"m_{m}"] = per_model[m].get(r["key"])
        if any(rec[f"m_{m}"] is not None for m in panel):
            records.append(rec)

    stats = {
        "reference_note": (
            "precision/recall are AGREEMENT vs a proxy-gold (no absolute truth): 'majority' = panel "
            "majority vote (leave-one-out for the model being scored); 'combined' = the taxonomy-grounded "
            "combined-automated verdict. abstain (Judge A/B None or a model's unparseable output) is a "
            "recall miss, never a precision positive; abstain_rate is reported per judge."
        ),
        "panel": panel,
        "n_records": len(records),
        "per_model_labeled": {m: sum(v is not None for v in per_model[m].values()) for m in panel},
        "per_model_abstain_rate": {
            m: round(1 - (sum(1 for r in records if r[f"m_{m}"] is not None) / len(records)), 4)
            if records
            else None
            for m in panel
        },
        "by_category_majority": judge_stats.profile_panel(records, panel, "category", "majority"),
        "by_regime_majority": judge_stats.profile_panel(records, panel, "regime", "majority"),
        "by_category_combined": judge_stats.profile_panel(records, panel, "category", "combined"),
        "inter_judge_kappa": judge_stats.kappa_matrix(records, panel),
    }
    d["per_judge_stats"] = stats
    d["llm_validated"] = len(records) > 0
    Path(core_path).write_text(json.dumps(d, indent=2))
    print(f"\nDONE. n_records={len(records)} panel={panel}", flush=True)
    ov = stats["by_category_majority"]["overall"]
    for j, m in ov.items():
        print(
            f"  {j:22} precision={m['precision']} recall={m['recall']} f1={m['f1']} "
            f"n={m['n_scored']} abstain={m['abstain_rate']}",
            flush=True,
        )
    return d


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Multi-model per-judge validation (precision/recall/κ)."
    )
    ap.add_argument("--core", default=str(_OUTDIR / "harmonization_precision.json"))
    ap.add_argument("--n", type=int, default=4000, help="target distinct records per model")
    ap.add_argument(
        "--workers", type=int, default=4, help="concurrent requests (Ollama serialises 24B)"
    )
    ap.add_argument("--panel", default=",".join(_PANEL), help="comma-separated Ollama model tags")
    ap.add_argument("--labels", default=str(_OUTDIR / "judge_labels"))
    args = ap.parse_args()
    run(
        args.core,
        args.n,
        args.workers,
        [m.strip() for m in args.panel.split(",") if m.strip()],
        args.labels,
    )


if __name__ == "__main__":
    main()
