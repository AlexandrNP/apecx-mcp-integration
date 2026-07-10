"""Driver for the non-circular cross-index harmonization precision/recall eval.

Run:
  APECX_SYNONYM_DICT_PATH=~/.apecx/dictionary/dictionary.sqlite PYTHONPATH=src:. \
    .venv/bin/python -m tests.eval.harmonization.run_harmonization \
      --categories mu_virus,abbreviations,real_world --k 25 --llm-validate
  # smoke: --max-queries 3 --no-llm

Impure batch driver: it hits the live public Globus indices (anonymous read) + the dictionary + an
optional LLM. It NEVER raises on a per-cell error (captured on the cell) and writes a re-scorable JSON
so `metrics.py` can re-aggregate offline. Read-only w.r.t. production code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from apecx_integration.agents.globus_search._datacite import (
    datacite_organisms,
    datacite_primary_id,
    datacite_subjects,
    datacite_taxon_iris,
    datacite_title,
)
from apecx_integration.synonym_dictionary.loader import (
    configure_dictionary_path,
    get_dictionary_index,
)
from tests.eval.harmonization import judges, llm_validate, metrics
from tests.eval.harmonization.probe import ALL_INDICES, probe_cell
from tests.eval.harmonization.resolve import resolve_query

_QDIR = Path(__file__).parent / "queries"
_OUTDIR = Path(__file__).parent / "output"
_CAT_FILES = {
    "mu_virus": "mu_virus.txt",
    "abbreviations": "abbreviations.txt",
    "real_world": "real_world.txt",
}


def _load_queries(category: str) -> list[str]:
    lines = (_QDIR / _CAT_FILES[category]).read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _record_id(content) -> str:
    """Stable identity for pooling (dedup raw ∪ harm). Prefer the object citation token; fall back to a
    hash of all sorted identifiers (a record with no primary id still dedups against itself)."""
    pid = datacite_primary_id(content)
    if pid:
        return pid
    from apecx_integration.agents.globus_search._datacite import datacite_identifiers

    flat = sorted(f"{k}:{v}" for k, vs in datacite_identifiers(content).items() for v in vs)
    if flat:
        return "ids:" + hashlib.sha1("|".join(flat).encode()).hexdigest()[:16]
    return "title:" + hashlib.sha1((datacite_title(content) or "").encode()).hexdigest()[:16]


def _gold_ids(records: list, subtree: set[int], synonyms) -> tuple[set[str], dict[str, list]]:
    """Judge a pooled record set for RECALL gold. Judge A first (O(1) subtree membership); only fall to
    the text Judge B when A abstains (structural records). Returns (relevant_ids, id->content index)."""
    gold: set[str] = set()
    index: dict[str, list] = {}
    for r in records:
        rid = _record_id(r)
        index.setdefault(rid, r)
        a = judges.judge_a(r, subtree)
        if a is True or a is None and judges.judge_b(r, synonyms) is True:
            gold.add(rid)
    return gold, index


def _stratified_sample(records: list, k: int) -> list:
    """Deterministic stratified sample of served records: split by (has-source-id, valueUri>1), then
    round-robin so each stratum is represented. Deterministic given the record order (no RNG)."""
    if len(records) <= k:
        return list(records)
    strata: dict[tuple, list] = {}
    for r in records:
        key = (bool(judges.source_taxon_ids(r)), len(datacite_taxon_iris(r)) > 1)
        strata.setdefault(key, []).append(r)
    out: list = []
    buckets = [iter(v) for v in strata.values()]
    while len(out) < k and buckets:
        for it in list(buckets):
            try:
                out.append(next(it))
                if len(out) >= k:
                    break
            except StopIteration:
                buckets.remove(it)
    return out


def _judge_cell(cell, subtree: set[int], synonyms, k: int) -> dict:
    """Sample the served records, run the combined non-circular judge, classify FPs, score precision +
    pool-relative recall. Returns the per-cell result dict (re-scorable)."""
    pool = list({_record_id(r): r for r in (cell.raw_records + cell.harm_records)}.values())
    gold, _ = _gold_ids(pool, subtree, synonyms)
    raw_ids = {_record_id(r) for r in cell.raw_records}
    harm_ids = {_record_id(r) for r in cell.harm_records}
    served_ids = {_record_id(r) for r in cell.served_records}

    # Oversample the raw-substitution (broken/miss) cells — that is where precision degrades and a
    # confident denominator matters most — but BOUND it (never judge a whole 10k corpus). Everything
    # honors --k as the base; the header's recorded ``k`` now controls sampling.
    sample_k = min(len(cell.served_records), 4 * k) if cell.served_from_raw else k
    sample = _stratified_sample(cell.served_records, sample_k)
    verdicts: list[str] = []
    fp_breakdown: dict[str, int] = {}
    sample_rows: list[dict] = []
    for r in sample:
        a = judges.judge_a(r, subtree)
        b = judges.judge_b(r, synonyms)
        v = judges.combined_verdict(a, b)
        verdicts.append(v)
        # Carry the record's identifiable TEXT (not just the id) so the JSON is self-contained for
        # offline re-scoring AND the LLM validation has real signal to judge, not a bare token.
        row = {
            "primary_id": datacite_primary_id(r),
            "title": datacite_title(r),
            "organism": "; ".join(datacite_organisms(r)) or None,
            "subjects": "; ".join(datacite_subjects(r, limit=8)) or None,
            "source_taxa": judges.source_taxon_ids(r),
            "n_valueuri": len(datacite_taxon_iris(r)),
            "judge_a": a,
            "judge_b": b,
            "verdict": v,
        }
        if v in ("false_positive", "disagree"):
            fp = judges.classify_fp(r, cell.served_from_raw, len(datacite_taxon_iris(r)))
            fp_breakdown[fp] = fp_breakdown.get(fp, 0) + 1
            row["fp_class"] = fp
        sample_rows.append(row)

    prec = metrics.precision(verdicts)
    rec = metrics.recall_fractions(raw_ids, harm_ids, served_ids, gold)
    return {
        "index": cell.index,
        "raw_total": cell.raw_total,
        "harm_total": cell.harm_total,
        "served_verdict": cell.verdict,
        "served_from_raw": cell.served_from_raw,
        "capped": cell.capped,
        "error": cell.error,
        "precision": prec,
        "recall": rec,
        "f1_served": metrics.f1(prec["precision"], rec["served"]),
        "fp_breakdown": fp_breakdown,
        "sample": sample_rows,
    }


def run(
    categories: list[str],
    k: int,
    llm: bool,
    max_queries: int | None,
    fetch_limit: int,
    out_path: str,
) -> dict:
    import globus_sdk

    client = globus_sdk.SearchClient()
    index_obj, dict_err = get_dictionary_index()
    if index_obj is None:
        raise RuntimeError(f"dictionary unavailable: {dict_err}")

    query_snaps: list[dict] = []
    cells: list[dict] = []
    for cat in categories:
        terms = _load_queries(cat)
        if max_queries:
            terms = terms[:max_queries]
        for term in terms:
            rq = resolve_query(term, cat)
            snap = {
                "term": term,
                "category": cat,
                "regime": rq.regime,
                "resolved_term": rq.resolved_term,
                "path": rq.lookup.path,
                "canonical_iri": rq.lookup.canonical_iri,
                "canonical_label": rq.lookup.canonical_label,
                "n_candidates": len(rq.lookup.candidates),
            }
            if rq.regime == "ambiguous_paused":
                snap["correctly_paused"] = True  # no Globus query issued, precision N/A
                query_snaps.append(snap)
                continue
            query_snaps.append(snap)
            subtree = (
                judges.build_subtree(index_obj, rq.lookup.canonical_iri)
                if rq.lookup.canonical_iri
                else set()
            )
            synonyms = (
                rq.lookup.synonyms or (rq.lookup.canonical_label,)
                if rq.lookup.canonical_label
                else ()
            )
            for index in ALL_INDICES:
                cell = probe_cell(
                    client,
                    index,
                    term,
                    rq.lookup.canonical_iri,
                    rq.lookup.canonical_label,
                    fetch_limit,
                )
                scored = _judge_cell(cell, subtree, synonyms, k)
                scored.update({"term": term, "category": cat, "regime": rq.regime})
                cells.append(scored)

    # Aggregate over ALL cells: metrics.aggregate skips None precision + None recall-lift per cell, so a
    # cell whose sample was all-unjudgeable still contributes its (valid) recall lift — recall
    # aggregation is NOT coupled to precision-judgeability.
    n_resolved = len({c["term"] for c in cells})  # distinct pathogens actually probed (non-paused)
    result = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": llm_validate._MODEL if llm else None,
        "k": k,
        "fetch_limit": fetch_limit,  # recall is pool-relative at this depth: recall@fetch_limit
        "llm_validated": False,
        "n_queries": len(query_snaps),
        "n_cells": len(cells),
        "queries": query_snaps,
        "cells": cells,
        "aggregate": {
            "by_category": metrics.aggregate(cells, "category"),
            "by_regime": metrics.aggregate(cells, "regime"),
            "by_index": metrics.aggregate(cells, "index"),
        },
        "coverage": metrics.coverage_by_index(cells, n_resolved),
        "judge_agreement": None,
    }
    # Persist the CORE results (the expensive Globus data + all automated metrics) BEFORE the optional
    # LLM validation, so a slow/hung Ollama can never gate the deliverable. Validation then updates it.
    _OUTDIR.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(result, indent=2))
    if llm:
        agreement = _llm_validate(cells, query_snaps)
        if agreement:
            result["judge_agreement"] = agreement
            result["llm_validated"] = True
            Path(out_path).write_text(json.dumps(result, indent=2))
    return result


def _llm_validate(
    cells: list[dict], query_snaps: list[dict], per_regime_cap: int = 10, budget_s: float = 180.0
) -> dict | None:
    if not llm_validate.llm_available():
        return None
    deadline = time.monotonic() + budget_s  # hard wall-time cap so slow Ollama can't hang the run
    # Validate the SAMPLE ROWS we already scored, feeding the LLM the record's real text. Oversample
    # A/B disagreements (the ambiguous cases the automated judge is least sure about); add a per-regime
    # baseline of confident verdicts. Rows with no usable text are skipped (the LLM needs signal).
    label_by_term = {
        q["term"]: (q.get("canonical_label") or q["resolved_term"]) for q in query_snaps
    }
    pairs: list[tuple[str, bool | None]] = []
    seen_regime: dict[str, int] = {}
    for cell in cells:
        if time.monotonic() > deadline:
            break
        pathogen = label_by_term.get(cell["term"], cell["term"])
        for row in cell["sample"]:
            if time.monotonic() > deadline:
                break
            text = row.get("title") or row.get("organism") or row.get("subjects")
            if not text:
                continue
            want = row["verdict"] == "disagree" or (
                seen_regime.get(cell["regime"], 0) < per_regime_cap
            )
            if not want:
                continue
            seen_regime[cell["regime"]] = seen_regime.get(cell["regime"], 0) + 1
            verdict = llm_validate.llm_judge(
                row.get("title") or "",
                row.get("organism") or "",
                row.get("subjects") or "",
                pathogen,
            )
            pairs.append((row["verdict"], verdict["belongs"]))
    return metrics.judge_agreement(pairs)


def main() -> None:
    ap = argparse.ArgumentParser(description="Non-circular harmonization precision/recall eval.")
    ap.add_argument("--categories", default="mu_virus,abbreviations,real_world")
    ap.add_argument("--k", type=int, default=25)
    ap.add_argument(
        "--fetch-limit",
        type=int,
        default=None,
        help="records pulled per leg for the recall pool (recall@fetch_limit); default 1500",
    )
    ap.add_argument(
        "--max-queries", type=int, default=None, help="cap queries per category (smoke)"
    )
    ap.add_argument("--llm-validate", dest="llm", action="store_true")
    ap.add_argument("--no-llm", dest="llm", action="store_false")
    ap.add_argument("--out", default=str(_OUTDIR / "harmonization_precision.json"))
    ap.set_defaults(llm=True)
    args = ap.parse_args()

    dict_path = os.environ.get("APECX_SYNONYM_DICT_PATH") or str(
        Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"
    )
    configure_dictionary_path(dict_path)

    from tests.eval.harmonization.probe import _DEFAULT_FETCH_LIMIT

    fetch_limit = args.fetch_limit or _DEFAULT_FETCH_LIMIT
    # run() writes the core JSON before validation (crash/hang-safe) and updates it after.
    result = run(
        args.categories.split(","), args.k, args.llm, args.max_queries, fetch_limit, args.out
    )
    agg = result["aggregate"]["by_category"]
    print(f"wrote {args.out}  ({result['n_cells']} cells, {result['n_queries']} queries)")
    for cat, m in agg.items():
        print(
            f"  {cat}: precision={m['precision']} judged={m['judged']} "
            f"unjudgeable_rate={m['unjudgeable_rate']} mean_recall_lift={m['mean_recall_lift']} cells={m['n_cells']}"
        )
    print("PER-INDEX coverage + precision:")
    idxagg = result["aggregate"]["by_index"]
    for name, cov in result["coverage"].items():
        p = idxagg.get(name, {})
        print(
            f"  {name:24} coverage={cov['coverage_rate']} ({cov['pathogens_covered']}/{cov['pathogens_probed']}) "
            f"precision={p.get('precision')} recall_lift={p.get('mean_recall_lift')}"
        )
    if result["judge_agreement"]:
        print(f"  judge vs LLM: {result['judge_agreement']}")


if __name__ == "__main__":
    main()
