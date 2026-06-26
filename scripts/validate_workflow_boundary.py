#!/usr/bin/env python3
"""End-to-end MCP-tool-boundary validation harness for APECx workflows.

Invokes ``run_workflow`` THROUGH the tool surface with a recording ``ctx`` (exactly
as a desktop client does), captures the streamed notifications AND the returned
result, and asserts the acceptance contract on the boundary the USER sees:

  C1  every step valid           — each stage output non-empty, non-error, non-placeholder
  C2  progress visible in result — the report carries a per-stage section + the stream fired per stage
  C3  artifacts in the result    — referenced AND content reachable from the result (not just a disk path)
  C4  full report in the result  — complete deterministic markdown, not a scaffold/summary
  C5  every leg non-empty        — literature/PubMed, conservation, structural produced data
  C6  prereq honesty             — with Docker up, structural/SASA actually ran (no false "unavailable")

Usage:  PYTHONPATH=src .venv/bin/python scripts/validate_workflow_boundary.py [virus ...]
Writes a JSON dump per run to /tmp/wf_boundary_<virus>.json and prints a contract scorecard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys

# nanobrain floods INFO/DEBUG (~95 MB/min) into stderr — silence it so the harness output + the run
# aren't drowned. The flood itself is a backlog finding (server-log hygiene), NOT a client-facing
# C1-C6 failure (in the real server it goes to the server log, not the MCP result). Keep WARNING+ so
# degrade notes still surface.
logging.disable(logging.INFO)


class _Session:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def send_log_message(self, level=None, data=None, logger=None, **kw) -> None:
        self._sink.append({"level": level, "data": data})


class RecordingCtx:
    """Minimal stand-in for FastMCP's Context — records every notification."""

    def __init__(self) -> None:
        self.progress: list[tuple] = []
        self.logs: list[dict] = []
        self.session = _Session(self.logs)

    async def report_progress(self, progress=None, total=None, message=None) -> None:
        self.progress.append({"progress": progress, "message": message})


# Strings that mean "this step did not really produce output".
_PLACEHOLDERS = (
    "not available",
    "without docker",
    "wasn't computed",
    "was not computed",
    "not computed",
    "no data",
    "n/a",
    "placeholder",
    "todo",
    "tbd",
    "0 records",
    "no pubmed",
    "no literature",
    "no records retrieved",
)


async def run_one(virus: str) -> tuple[RecordingCtx, dict]:
    from apecx_integration.mcp_surface.locus import ExecutionLocus, set_active_locus
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    # The user's case is the desktop client.
    set_active_locus(ExecutionLocus.DESKTOP)
    ctx = RecordingCtx()
    result = await run_workflow("viral_epitope_analysis", {"query": virus}, ctx)
    return ctx, result


def assess(ctx: RecordingCtx, result: dict) -> dict:
    md = result.get("markdown") or ""
    md_l = md.lower()
    stage_logs = [
        log
        for log in ctx.logs
        if isinstance(log.get("data"), dict) and log["data"].get("event") == "stage_report"
    ]
    return {
        "C1_status": result.get("status"),
        "C1_error": result.get("error"),
        "C1_placeholder_hits": [p for p in _PLACEHOLDERS if p in md_l],
        "C2_stages_streamed": len(stage_logs),
        "C2_progress_pings": len(ctx.progress),
        "C2_report_has_steps_section": bool(re.search(r"##.*step", md_l)),
        "C3_artifact_dir": result.get("artifact_dir"),
        "C3_artifact_path": result.get("artifact_path"),
        "C3_artifact_content_in_result": any(
            "content" in k.lower() for k in result if "artifact" in k.lower()
        ),
        "C4_markdown_chars": len(md),
        "C4_looks_like_scaffold": (
            "host synthesis" in md_l or "synthesis scaffold" in md_l or len(md) < 600
        ),
        "C5_pubmed_empty_flag": bool(
            re.search(r"no pubmed|0 (pubmed|literature|publication|record)", md_l)
        ),
        "C6_structural_unavailable_flag": bool(
            re.search(r"sasa.*not available|not available.*docker|without docker", md_l)
        ),
        "result_keys": sorted(result.keys()),
        "stage_names": [log["data"].get("stage") for log in stage_logs],
    }


async def main() -> None:
    viruses = sys.argv[1:] or ["influenza"]
    scorecard = {}
    for v in viruses:
        print(f"\n===== {v} =====", flush=True)
        try:
            ctx, result = await run_one(v)
        except Exception as exc:  # noqa: BLE001 — the harness must survive a run crash
            import traceback

            print(f"  RUN CRASHED: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            scorecard[v] = {"crashed": f"{type(exc).__name__}: {exc}"}
            continue
        findings = assess(ctx, result)
        scorecard[v] = findings
        dump = {"result": result, "progress": ctx.progress, "logs": ctx.logs, "findings": findings}
        path = f"/tmp/wf_boundary_{v}.json"
        with open(path, "w") as fh:
            json.dump(dump, fh, indent=2, default=str)
        for k, val in findings.items():
            print(f"  {k}: {val}", flush=True)
        print(f"  (full dump: {path})", flush=True)
    print("\n===== CONTRACT SCORECARD =====", flush=True)
    print(json.dumps(scorecard, indent=2, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
