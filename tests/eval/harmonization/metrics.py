"""Pure metric arithmetic for the harmonization precision/recall eval.

No I/O, no network — every function is a pure transformation over already-collected verdicts/ids, so
the JSON output can be re-scored offline. The recall arithmetic is ported from the sibling benchmark
(apecx-harvesters-work/benchmarks/recall_oracle.py::_recall_fractions) and extended with a third
"served" leg (what harmonized_search actually returns to the user).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable


def recall_fractions(
    raw_ids: set[str], harm_ids: set[str], served_ids: set[str], gold_ids: set[str]
) -> dict[str, float | None]:
    """Pool-relative TREC recall for each leg over the independent pooled gold set.

    Ported from recall_oracle._recall_fractions (which returned only (raw, harm)); extended to the
    served leg. Gold = the pooled records an INDEPENDENT judge deemed relevant (built by the caller
    from raw ∪ harm, judged by judges.combined_verdict — NOT by the filtered subjects.valueUri field).
    Returns None for every leg when the gold pool is empty (recall undefined), never 0.0 (which would
    falsely read as "retrieved nothing relevant").
    """
    g = len(gold_ids)
    if not g:
        return {"before": None, "after": None, "served": None, "lift": None}
    before = round(len(raw_ids & gold_ids) / g, 4)
    after = round(len(harm_ids & gold_ids) / g, 4)
    served = round(len(served_ids & gold_ids) / g, 4)
    return {"before": before, "after": after, "served": served, "lift": round(after - before, 4)}


def precision(verdicts: Iterable[str]) -> dict[str, float | None]:
    """Precision over a judged sample. ``unjudgeable`` is EXCLUDED from the denominator (never folded
    in silently); its rate is reported alongside so a low-signal cell is visible, not hidden.

    ``verdicts`` are combined_verdict outputs: relevant | false_positive | disagree | unjudgeable.
    A ``disagree`` (Judge A vs B conflict) is counted as NOT relevant for the point estimate (it is an
    LLM-validation target, not a confident pass) but tracked separately.
    """
    c = Counter(verdicts)
    judged = c["relevant"] + c["false_positive"] + c["disagree"]
    total = judged + c["unjudgeable"]
    return {
        "precision": round(c["relevant"] / judged, 4) if judged else None,
        "judged": judged,
        "relevant": c["relevant"],
        "false_positive": c["false_positive"],
        "disagree": c["disagree"],
        "unjudgeable": c["unjudgeable"],
        "unjudgeable_rate": round(c["unjudgeable"] / total, 4) if total else None,
    }


def f1(p: float | None, r: float | None) -> float | None:
    """Harmonic mean of precision and recall; None if either is None or both are 0."""
    if p is None or r is None or (p + r) == 0:
        return None
    return round(2 * p * r / (p + r), 4)


def aggregate(cells: list[dict], key: str) -> dict[str, dict]:
    """Micro-mean precision + mean recall-lift grouped by ``cell[key]`` (e.g. 'category' or 'regime').

    Micro-mean = pool all judged records in the group (a cell with more records weighs more), which is
    the honest corpus-level precision — NOT a macro-mean over cells (which would let a 1-record cell
    swing the number). Recall-lift is meaned over cells that HAVE a defined lift.
    """
    groups: dict[str, dict] = {}
    for cell in cells:
        g = groups.setdefault(
            cell[key], {"relevant": 0, "judged": 0, "unjudgeable": 0, "lifts": [], "n_cells": 0}
        )
        pr = cell.get("precision") or {}
        g["relevant"] += pr.get("relevant", 0)
        g["judged"] += pr.get("judged", 0)
        g["unjudgeable"] += pr.get("unjudgeable", 0)
        lift = (cell.get("recall") or {}).get("lift")
        if lift is not None:
            g["lifts"].append(lift)
        g["n_cells"] += 1
    out = {}
    for name, g in groups.items():
        seen = g["judged"] + g["unjudgeable"]
        out[name] = {
            "precision": round(g["relevant"] / g["judged"], 4) if g["judged"] else None,
            "judged": g["judged"],
            # A precision headline is only as trustworthy as this rate is low — surfaced so a clean
            # precision on a thin judged base (many unjudgeable records) cannot masquerade as strong.
            "unjudgeable_rate": round(g["unjudgeable"] / seen, 4) if seen else None,
            "mean_recall_lift": round(sum(g["lifts"]) / len(g["lifts"]), 4) if g["lifts"] else None,
            "n_cells": g["n_cells"],
        }
    return out


def coverage_by_index(cells: list[dict], n_resolved: int) -> dict[str, dict]:
    """Per-index coverage assessment: which indices actually hold data for which pathogens.

    For each index: how many distinct pathogens it returns ≥1 harmonized record for (``harm_total > 0``)
    out of the ``n_resolved`` probed (non-paused) pathogens, plus its served-verdict profile — so a
    systemically ``broken`` index (a stale-dict / ICTV-rename concentration) shows up as a column, not
    buried per-query. ``covered_terms`` is kept for the reverse per-pathogen view.
    """
    idx: dict[str, dict] = {}
    for cell in cells:
        d = idx.setdefault(
            cell["index"], {"covered_terms": set(), "verdicts": Counter(), "n_cells": 0}
        )
        d["n_cells"] += 1
        d["verdicts"][cell.get("served_verdict", "")] += 1
        if cell.get("harm_total", 0) > 0:
            d["covered_terms"].add(cell["term"])
    out = {}
    for name, d in idx.items():
        covered = len(d["covered_terms"])
        out[name] = {
            "pathogens_covered": covered,
            "pathogens_probed": n_resolved,
            "coverage_rate": round(covered / n_resolved, 4) if n_resolved else None,
            "verdicts": dict(d["verdicts"]),
            "n_cells": d["n_cells"],
        }
    return out


def judge_agreement(pairs: list[tuple[str, bool | None]]) -> dict[str, float | None]:
    """Cohen's κ + accuracy of the automated verdict vs the LLM gold, over a validation sample.

    ``pairs`` = [(automated_verdict, llm_belongs)] where automated_verdict ∈ {relevant,false_positive,
    disagree,unjudgeable} and llm_belongs ∈ {True, False, None}. LLM ``None`` (error/unparseable) rows
    are dropped from κ (no gold), but counted in ``llm_abstained`` so they are never silently ignored.
    Automated is binarized: relevant → True, false_positive/disagree/unjudgeable → False (the point
    estimate treats a non-confident verdict as not-relevant).
    """
    a_bin: list[bool] = []
    g_bin: list[bool] = []
    abstained = 0
    for automated, llm in pairs:
        if llm is None:
            abstained += 1
            continue
        a_bin.append(automated == "relevant")
        g_bin.append(bool(llm))
    n = len(a_bin)
    if n == 0:
        return {"accuracy": None, "kappa": None, "n": 0, "llm_abstained": abstained}
    agree = sum(1 for a, g in zip(a_bin, g_bin, strict=True) if a == g)
    p_o = agree / n
    # Chance agreement from each rater's marginal True-rate.
    pa_t, pg_t = sum(a_bin) / n, sum(g_bin) / n
    p_e = pa_t * pg_t + (1 - pa_t) * (1 - pg_t)
    kappa = None if p_e == 1.0 else round((p_o - p_e) / (1 - p_e), 4)
    return {"accuracy": round(p_o, 4), "kappa": kappa, "n": n, "llm_abstained": abstained}
