"""Gated LLM-as-judge that VALIDATES the automated precision judge on a stratified sample.

We do NOT let the LLM produce the precision number (that would be slow + non-reproducible over 140
queries). Instead the cheap automated judges (judges.py) score every record, and this LLM validates a
small stratified sub-sample so the headline precision carries a measured confidence (accuracy + Cohen κ
vs this gold) rather than a bare claim (loop-benchmark discipline: distrust the metric before trusting
it). Adapted from apecx-harvesters-work/benchmarks/precision_audit.py::llm_judge.

Gated: runs only when the apecx LLM backend answers. ``belongs=None`` on any LLM/parse failure is
carried through (never silently dropped, never a fabricated verdict), and such rows are excluded from κ.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

_BASE = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
# Default to the strongest locally-available judge (24B) — the prior 4B model gave κ=0.13 on n=43, a
# weak external check. Override with APECX_LLM_MODEL. The judge's own reliability bounds κ's meaning.
_MODEL = os.environ.get("APECX_LLM_MODEL", "devstral:24b")
_KEY = os.environ.get("APECX_LLM_API_KEY", "")
_CACHE: dict[tuple[str, str], dict] = {}

_SYSTEM = (
    "You are a virologist adjudicating a bioinformatics search result. Decide whether a database "
    "record with the given title/organism/keywords is genuinely ABOUT the requested pathogen, as "
    "opposed to a different but similarly-named or merely related organism (a different species in "
    "the same family, a bound host/antibody partner, or a name collision). Judge by naming + taxonomy "
    'only. Respond ONLY with JSON: {"belongs": true or false, "reason": "<one short sentence>"}.'
)


def llm_available(timeout: int = 5) -> bool:
    """Cheap reachability probe (an empty /models or a 1-token completion)."""
    try:
        req = urllib.request.Request(f"{_BASE}/models", headers=_headers())
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _KEY:
        h["Authorization"] = f"Bearer {_KEY}"
    return h


def _signature(title: str, organism: str, subjects: str) -> str:
    return " | ".join(p for p in (title, organism, subjects) if p)[:300]


def llm_judge(
    title: str,
    organism: str,
    subjects: str,
    pathogen: str,
    *,
    model: str | None = None,
    timeout: int = 20,
) -> dict:
    """Return {"belongs": bool|None, "reason": str}. Cached per (record-signature, pathogen, model) — the
    same record recurs across strata, and the panel judges each record with several models, so each
    distinct (record, pathogen, model) is judged once. ``belongs=None`` (error/unparse) is NOT cached
    (may retry) and never counts as a verdict."""
    model = model or _MODEL
    sig = _signature(title, organism, subjects)
    key = (sig, pathogen, model)
    if key in _CACHE:
        return _CACHE[key]
    user = (
        f"REQUESTED pathogen: {pathogen}\n"
        f"record title: {title or '(none)'}\n"
        f"record organism(s): {organism or '(none)'}\n"
        f"record keywords: {subjects or '(none)'}\n"
        "Is this record genuinely about the requested pathogen?"
    )
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        }
    ).encode()
    try:
        req = urllib.request.Request(f"{_BASE}/chat/completions", data=body, headers=_headers())
        resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        content = resp["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return {"belongs": None, "reason": f"LLM error: {type(exc).__name__}"}  # not cached
    parsed = _extract_json(content)
    # Prefer JSON; fall back to prose parsing for models that answer in prose (some bio finetunes).
    belongs = bool(parsed["belongs"]) if parsed and "belongs" in parsed else _prose_belongs(content)
    if belongs is None:
        return {"belongs": None, "reason": f"unparseable: {content[:80]}"}  # not cached
    out = {"belongs": belongs, "reason": (parsed or {}).get("reason", content[:120])}
    _CACHE[key] = out
    return out


def _extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _prose_belongs(text: str) -> bool | None:
    """Fallback verdict for models that answer in PROSE, not JSON (e.g. medllama2: "the answer is
    false."). Conservative + high-precision — better to abstain (None) than mis-verdict, since these
    feed a judge's precision/recall. Guards three false-positive traps found in review: a bare schema
    echo ``{"belongs": true or false}``, a hedge "...true or false", and "about a different X, not the
    query" (a semantic negative that a naive "is about" match would call positive)."""
    t = text.lower()
    # Schema-echo / instruction parrot / hedge that lists both options → NOT a verdict.
    if (
        "respond only with json" in t
        or "adjudicating a bioinformatics" in t
        or "true or false" in t
    ):
        return None
    # Negative verdicts first — a negation of aboutness dominates a co-occurring "is about".
    if (
        re.search(r"answer is[:\-\s]*false\b", t)
        or re.search(r'"?belongs"?\s*[:=]\s*false\b', t)
        or re.search(
            r"\bnot\s+(genuinely\s+)?about\b", t
        )  # "not (genuinely) about" — not "not sure about"
        or "not the requested" in t
        or "not the query" in t
        or re.search(r"\bdifferent\s+(species|virus|organism|pathogen|flavivirus|alphavirus)", t)
        or "name collision" in t
    ):
        return False
    # Positive verdicts — must affirm aboutness of THE requested/query pathogen, not "a different" one.
    if (
        re.search(r"answer is[:\-\s]*true\b", t)
        or re.search(r'"?belongs"?\s*[:=]\s*true\b', t)
        or re.search(r"\babout the (requested|query)\b", t)
        or re.search(r"genuinely about the (requested|query)", t)
        or re.search(r"^\s*yes\b", t)
    ):
        return True
    return None
