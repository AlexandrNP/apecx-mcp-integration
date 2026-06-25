"""The impure e2e driver for the epitope eval — run viral_epitope_analysis for one query through the REAL
stack and score the reason-aware checks. Captures: step events (streaming), the on-disk artifact dir, the
report markdown, and the proceed-notes (the degrade ledger that makes empty-artifact checks honest).

Run entry is the framework-native scientist surface: eo_primitives.run_workflow("viral_epitope_analysis",
{...}). Ollama/MAFFT-gated by the caller. Never raises — returns an EpitopeResult always.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from tests.eval.epitope_checks import (
    CheckResult,
    check_completeness,
    check_full_artifacts,
    check_protabank_reported,
    check_report_references,
    check_streaming,
    protabank_count,
)

_WS = "/Users/onarykov/Downloads/apecx-cowork"
_TRANSIENT = (
    "timeout",
    "timed out",
    "connection",
    "reset",
    "refused",
    "unreachable",
    "503",
    "502",
    "eof",
)


@dataclass
class EpitopeResult:
    query: str
    status: str | None = None
    run_id: str | None = None
    markdown: str | None = None
    events_by_step: dict[str, set] = field(default_factory=dict)
    run_dir: Path | None = None
    proceed_stages: set[str] = field(default_factory=set)
    protabank: int | None = None
    checks: list[CheckResult] = field(default_factory=list)
    error: str | None = None
    transient: bool = False

    @property
    def passed(self) -> bool:
        return not self.error and bool(self.checks) and all(c.passed for c in self.checks)


def _is_transient(msg: str | None) -> bool:
    return any(t in (msg or "").lower() for t in _TRANSIENT)


def _ensure_env() -> None:
    os.environ.setdefault("APECX_LLM_BASE_URL", "http://localhost:11434/v1")
    os.environ.setdefault("APECX_LLM_MODEL", "nemotron-3-nano:4b")
    os.environ.setdefault("APECX_LLM_API_KEY", "EMPTY")
    os.environ.setdefault("APECX_WORKSPACE_ROOT", _WS)
    os.environ.setdefault("APECX_DATA_ROOT", f"{_WS}/data")
    os.environ.setdefault("APECX_SYNONYM_DICT_PATH", f"{_WS}/dictionary.sqlite")


_EXPECTED: set[str] | None = None


def _expected_steps() -> set[str]:
    global _EXPECTED
    if _EXPECTED is None:
        from apecx_integration.composition.workflows.viral_epitope_analysis.builder import (
            build_viral_epitope_analysis_workflow,
        )

        _EXPECTED = set(build_viral_epitope_analysis_workflow().child_steps)
    return _EXPECTED


def run_epitope(query: str, protein: str | None = None) -> EpitopeResult:
    """Drive viral_epitope_analysis for `query` (+ optional `protein`) and score the 5 reason-aware checks.
    The protein is a SEPARATE param (not embedded in the query) — the sequence-conservation + figure legs
    need it to fetch per-strain sequences; without it they degrade-loud (correctly). Never raises."""
    from nanobrain.core.step_events import subscribe_to_step_events

    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    _ensure_env()
    art = tempfile.mkdtemp(prefix="epitope_eval_")
    os.environ["APECX_ARTIFACTS_DIR"] = art
    events: dict[str, set] = defaultdict(set)
    failures: list[str] = []

    def cap(e) -> None:
        events[e.step_name].add(e.event_type)
        if e.event_type == "step_failed":
            msg = (e.payload or {}).get("exception", {}).get("message", "")
            failures.append(f"{e.step_name}: {msg}")

    payload = {"query": query}
    if protein:
        payload["protein"] = protein
    try:
        with subscribe_to_step_events(cap):
            out = asyncio.run(run_workflow("viral_epitope_analysis", payload))
    except Exception as exc:  # never raise out
        msg = f"{type(exc).__name__}: {exc}"
        return EpitopeResult(
            query,
            error=msg,
            transient=_is_transient(msg),
            events_by_step={k: set(v) for k, v in events.items()},
        )

    run_id = out.get("run_id")
    md = out.get("markdown")
    run_dir = Path(art) / str(run_id) if run_id else None

    proceed_stages: set[str] = set()
    if run_dir and (run_dir / "data.json").is_file():
        try:
            data = json.loads((run_dir / "data.json").read_text())
            notes = (data.get("parts") or {}).get("proceed_notes") or []
            proceed_stages = {n.get("stage", "") for n in notes if isinstance(n, dict)}
        except Exception:
            pass

    expected = _expected_steps()
    ev = {k: set(v) for k, v in events.items()}
    checks = [
        check_streaming(expected, ev),
        check_completeness(expected, ev),
        check_full_artifacts(run_dir, proceed_stages)
        if run_dir
        else CheckResult("full_artifacts", False, "no run_dir / no run_id"),
        check_report_references(md),
        check_protabank_reported(md),
    ]
    err = out.get("error")
    # RHEA is fail-closed REQUIRED for the protein/sequence leg (by design). When it's down, the 'align'
    # step raises and the run errors. That is ENVIRONMENT (RHEA not running), not a code bug — flag it so
    # the loop classifies it informational, not a gated silent-failure (the reason-aware distinction).
    rhea_down = any(
        "rhea" in f.lower() and ("reachable" in f.lower() or "workflow_output" in f.lower())
        for f in failures
    )
    if out.get("status") == "error" and rhea_down:
        err = "rhea_unavailable: " + (
            failures[0] if failures else "RHEA server down (sequence leg requires it)"
        )
    return EpitopeResult(
        query,
        status=out.get("status"),
        run_id=run_id,
        markdown=md,
        events_by_step=ev,
        run_dir=run_dir,
        proceed_stages=proceed_stages,
        protabank=protabank_count(md),
        checks=checks,
        error=err,
        transient=_is_transient(err),
    )
