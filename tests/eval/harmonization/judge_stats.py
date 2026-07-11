"""Per-judge precision / recall / F1 for the harmonization relevance judges, against a proxy-gold reference.

Every judge here answers the SAME binary question — "is this returned record about the queried pathogen?"
— from different evidence: **Judge A** (source NCBI-Taxonomy id ∈ queried subtree), **Judge B**
(title/organism synonym text), the **Combined** verdict, and the **LLM** (`devstral:24b`). There is no
absolute ground truth, so precision/recall here are AGREEMENT metrics against a REFERENCE judge:

  - for the automated judges (A, B, Combined) the reference is the **LLM** (the independent validator);
  - for the LLM the reference is the **Combined** automated verdict.

They answer: "when this judge says relevant, how often does the reference agree (**precision**); and of the
reference's relevants, how many does this judge catch (**recall**)" — NOT absolute correctness. Read
alongside Cohen κ (same proxy-gold caveat). An ABSTAIN (Judge A/B may return None) counts as a miss for
recall (it did not catch a reference-relevant) and is never a positive for precision; the abstain rate is
reported separately so a low-recall affirm-only judge (Judge B) is interpretable.

Pure: no I/O. Operates on already-collected per-record labels (each row carries ja, jb, verdict, llm).
"""

from __future__ import annotations

# --- binary relevance call per judge: True (relevant) / False (not-relevant) / None (abstain) ---


def call_judge_a(ja):
    return ja  # already True / False / None


def call_judge_b(jb):
    # Affirm-only by design: Judge B asserts relevance or abstains, never "not relevant".
    return True if jb is True else None


def call_combined(verdict):
    return {"relevant": True, "false_positive": False}.get(
        verdict
    )  # disagree/unjudgeable → None (abstain)


def call_llm(belongs):
    return belongs  # True / False / None


def _confusion(pairs) -> dict:
    """Confusion + precision/recall/F1/accuracy over resolved (call, ref) pairs (each True/False/None).
    A None reference is excluded (counted); an abstaining judge (call None) on a ref-relevant row is an
    FN, on a ref-not row a TN — abstention lowers recall but never inflates precision."""
    tp = fp = fn = tn = abstain = ref_undef = 0
    for c, r in pairs:
        if r is None:
            ref_undef += 1
            continue
        if c is None:
            abstain += 1
            fn += 1 if r is True else 0
            tn += 1 if r is False else 0
            continue
        if c and r:
            tp += 1
        elif c and not r:
            fp += 1
        elif (not c) and r:
            fn += 1
        else:
            tn += 1
    scored = tp + fp + fn + tn
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    acc = (tp + tn) / scored if scored else None
    return {
        "precision": round(prec, 4) if prec is not None else None,
        "recall": round(rec, 4) if rec is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "accuracy": round(acc, 4) if acc is not None else None,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "n_scored": scored,
        "abstained": abstain,
        "abstain_rate": round(abstain / scored, 4) if scored else None,
        "ref_undefined": ref_undef,
    }


def _prf(rows, judge_call, ref_call) -> dict:
    """Single-reference confusion: resolve each (judge_raw, ref_raw) row via the two call fns, then
    ``_confusion``. Kept for the single-LLM ``profile`` view; the panel path calls ``_confusion`` direct."""
    return _confusion((judge_call(jr), ref_call(rr)) for jr, rr in rows)


# (judge name) → (raw-field key, judge_call, reference raw-field key, ref_call). Reference = LLM for the
# automated judges; = the Combined automated verdict for the LLM.
_JUDGES = {
    "judge_a": ("judge_a", call_judge_a, "llm", call_llm),
    "judge_b": ("judge_b", call_judge_b, "llm", call_llm),
    "combined": ("verdict", call_combined, "llm", call_llm),
    "llm": ("llm", call_llm, "verdict", call_combined),
}


def profile_group(records: list[dict]) -> dict[str, dict]:
    """Every judge's precision/recall/F1/confusion over one group of per-record label dicts (each with
    keys ja→``judge_a``, jb→``judge_b``, ``verdict``, ``llm``)."""
    out: dict[str, dict] = {}
    for name, (jkey, jcall, rkey, rcall) in _JUDGES.items():
        out[name] = _prf(((rec.get(jkey), rec.get(rkey)) for rec in records), jcall, rcall)
    return out


def profile(records: list[dict], group_key: str | None = None) -> dict:
    """Per-judge metrics for every ``record[group_key]`` bucket (e.g. 'category' or 'regime'), plus an
    ``overall`` bucket over all records. ``group_key=None`` returns only ``overall``."""
    result: dict[str, dict] = {"overall": profile_group(records)}
    if group_key:
        groups: dict[str, list] = {}
        for rec in records:
            groups.setdefault(rec.get(group_key, "?"), []).append(rec)
        for g, rows in groups.items():
            result[g] = profile_group(rows)
    return result


# ==================================================================================================
# Multi-model PANEL: each of the 3 automated judges + N LLM models is a judge. A model's per-record
# verdict lives under key ``m_<model>`` (True/False/None). References: the panel-majority vote (the
# proxy-gold) and the combined-automated anchor. No absolute ground truth — AGREEMENT metrics (κ caveat).
# ==================================================================================================


def _majority(votes: list) -> bool | None:
    """>half True → True; >half False → False; tie or no votes → None (reference abstains)."""
    if not votes:
        return None
    t = sum(1 for v in votes if v)
    f = len(votes) - t
    return None if t == f else t > f


def panel_majority(record: dict, models: list[str], exclude: str | None = None) -> bool | None:
    """Majority relevance vote of the LLM panel for a record. ``exclude`` drops one model (leave-one-out
    when scoring that model, so it is never graded against a reference that counts its own vote)."""
    votes = [record[f"m_{m}"] for m in models if m != exclude and record.get(f"m_{m}") is not None]
    return _majority(votes)


def judge_binary(name: str, record: dict, models: list[str]) -> bool | None:
    """A judge's binary relevance call for a record — an automated judge (judge_a/judge_b/combined) or a
    model (→ its ``m_<name>`` verdict)."""
    if name == "judge_a":
        return call_judge_a(record.get("judge_a"))
    if name == "judge_b":
        return call_judge_b(record.get("judge_b"))
    if name == "combined":
        return call_combined(record.get("verdict"))
    return record.get(f"m_{name}")


def judge_names(models: list[str]) -> list[str]:
    return ["judge_a", "judge_b", "combined", *models]


def _cohen_kappa(pairs: list) -> float | None:
    """Cohen κ over (bool, bool) pairs (chance-corrected agreement); None when undefined (p_e==1)."""
    n = len(pairs)
    if n == 0:
        return None
    p_o = sum(1 for a, b in pairs if a == b) / n
    pa = sum(1 for a, _ in pairs if a) / n
    pb = sum(1 for _, b in pairs if b) / n
    p_e = pa * pb + (1 - pa) * (1 - pb)
    return None if p_e == 1.0 else round((p_o - p_e) / (1 - p_e), 4)


def profile_panel(
    records: list[dict],
    models: list[str],
    group_key: str | None = None,
    reference: str = "majority",
) -> dict:
    """Per-judge precision/recall/F1/confusion for EVERY judge (3 automated + each model), per
    ``group_key`` bucket + ``overall``. ``reference='majority'`` = panel-majority (leave-one-out for a
    model); ``reference='combined'`` = the combined-automated verdict (the taxonomy anchor)."""

    def ref_for(name: str, record: dict):
        if reference == "combined":
            return call_combined(record.get("verdict"))
        exclude = name if name in models else None
        return panel_majority(record, models, exclude=exclude)

    def group_profile(rows: list[dict]) -> dict:
        return {
            name: _confusion((judge_binary(name, r, models), ref_for(name, r)) for r in rows)
            for name in judge_names(models)
        }

    result: dict[str, dict] = {"overall": group_profile(records)}
    if group_key:
        groups: dict[str, list] = {}
        for r in records:
            groups.setdefault(r.get(group_key, "?"), []).append(r)
        for g, rows in groups.items():
            result[g] = group_profile(rows)
    return result


def kappa_matrix(records: list[dict], models: list[str]) -> dict[str, dict]:
    """Pairwise Cohen κ among ALL judges (automated + models), over records where BOTH are defined
    (abstains dropped pairwise). Symmetric; diagonal 1.0. Reveals which judges cluster."""
    names = judge_names(models)
    calls = {name: [judge_binary(name, r, models) for r in records] for name in names}
    mat: dict[str, dict] = {}
    for a in names:
        row: dict[str, float | None] = {}
        for b in names:
            if a == b:
                row[b] = 1.0
                continue
            pairs = [
                (ca, cb)
                for ca, cb in zip(calls[a], calls[b], strict=True)
                if ca is not None and cb is not None
            ]
            row[b] = _cohen_kappa(pairs)
        mat[a] = row
    return mat
