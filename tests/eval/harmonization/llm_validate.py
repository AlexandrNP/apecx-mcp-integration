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
import urllib.error
import urllib.request

_BASE = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
_MODEL = os.environ.get("APECX_LLM_MODEL", "nemotron-3-nano:4b")
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
    title: str, organism: str, subjects: str, pathogen: str, *, timeout: int = 20
) -> dict:
    """Return {"belongs": bool|None, "reason": str}. Cached per (record-signature, pathogen); the same
    record recurs across strata, so each distinct pair is judged once. ``belongs=None`` (error/unparse)
    is NOT cached (may retry) and never counts as a verdict."""
    sig = _signature(title, organism, subjects)
    key = (sig, pathogen)
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
            "model": _MODEL,
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
    if not parsed or "belongs" not in parsed:
        return {"belongs": None, "reason": f"unparseable: {content[:80]}"}  # not cached
    out = {"belongs": bool(parsed["belongs"]), "reason": str(parsed.get("reason", ""))[:200]}
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
