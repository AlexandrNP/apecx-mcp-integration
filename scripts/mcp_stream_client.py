"""A REAL stdio MCP client that consumes the desktop streaming split (E3-5).

Closes the honest gap left by E2-S: the ``run_workflow_streaming`` tool was only
ever driven by a fake ``Context`` in unit tests — no real MCP client had ever
consumed its stream over the wire. This script launches the ``apecx-mcp`` server
as a subprocess over stdio, does the MCP ``initialize`` handshake, registers BOTH
a logging-notification handler (the per-stage ``send_log_message`` payloads) and a
progress handler (the per-stage ``report_progress`` counter), then calls
``run_workflow_streaming`` and collects the notifications IN ARRIVAL ORDER.

The two notification channels map to the server's two-notification-per-stage shape
(``tools/eo_primitives.py::run_workflow_streaming``):

- progress   ← ``ctx.report_progress(progress=n, message="stage complete: <stage>")``
- log (info) ← ``ctx.session.send_log_message(data={event, stage, order, markdown,
               data, step_name, run_id})``

The ``mcp`` (1.27) client API wiring used here, verified against the installed
package (do not guess — these signatures are real):

- ``ClientSession(read, write, logging_callback=<async (params) -> None>)`` — the
  ``params`` is a ``LoggingMessageNotificationParams`` whose ``.data`` carries the
  server's ``send_log_message`` ``data=`` payload.
- ``session.call_tool(name, arguments, progress_callback=<async (progress, total,
  message) -> None>)`` — passing ``progress_callback`` makes the client mint a
  ``progressToken`` automatically (``shared/session.py`` uses the request id), which
  is exactly what the server's ``report_progress`` needs to not no-op.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/mcp_stream_client.py "<query>"

Heavy server startup (Control Plane autostart, synonym-dict build, infra
orchestrator) is switched off via env so the subprocess reaches ``server.run()``
quickly — the workflow itself still hits the REAL LLM / Globus / MAFFT / BV-BRC.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, LoggingMessageNotificationParams

WORKFLOW_NAME = "viral_epitope_evidence_review"

# Env that keeps the spawned server's boot lean: skip the Control Plane health
# check + backend autostart, skip the 10-15 min synonym-dictionary build, and run
# the infra orchestrator probe-only. None of these are needed by the evidence
# workflow (it talks to Globus / LLM / MAFFT / BV-BRC, not the Control Plane).
_LEAN_SERVER_ENV = {
    "APECX_MCP_SKIP_HEALTHCHECK": "1",
    "APECX_SKIP_DICT_BUILD": "1",
    "APECX_MCP_AUTOSTART_INFRA": "0",
    "PYTHONUNBUFFERED": "1",
}


@dataclass
class StreamEvent:
    """One notification as it arrived over the wire, tagged with arrival sequence."""

    seq: int
    kind: str  # "progress" | "log"
    stage: str | None
    payload: dict[str, Any]


@dataclass
class StreamRun:
    """Everything one streamed round-trip produced, in arrival order."""

    events: list[StreamEvent] = field(default_factory=list)
    result: dict[str, Any] | None = None
    handler_errors: list[str] = field(default_factory=list)

    @property
    def log_events(self) -> list[StreamEvent]:
        return [e for e in self.events if e.kind == "log"]

    @property
    def progress_events(self) -> list[StreamEvent]:
        return [e for e in self.events if e.kind == "progress"]

    @property
    def stage_reports(self) -> list[dict[str, Any]]:
        """The per-stage ``{stage, order, markdown, data, step_name, run_id}`` dicts,
        recovered from the log notifications in arrival order (the ``event`` tag
        stripped)."""
        out: list[dict[str, Any]] = []
        for e in self.log_events:
            data = e.payload
            if isinstance(data, dict) and data.get("event") == "stage_report":
                out.append({k: v for k, v in data.items() if k != "event"})
        return out


def _server_params() -> StdioServerParameters:
    """Launch ``apecx-mcp`` via this interpreter's ``-m`` form, inheriting the full
    env (so APECX_LLM_* / Globus creds / PATH reach the workflow) plus the lean-boot
    overrides. ``StdioServerParameters(env=...)`` REPLACES the child env wholesale —
    it does not merge — so we merge ``os.environ`` ourselves here."""
    env = {**os.environ, **_LEAN_SERVER_ENV}
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "apecx_integration.mcp_surface.server"],
        env=env,
    )


def _result_to_dict(result: CallToolResult) -> dict[str, Any]:
    """Recover the tool's returned dict from a CallToolResult.

    FastMCP serializes a dict-returning tool into ``structuredContent`` (preferred)
    and a JSON ``TextContent`` mirror. Read the structured form first, fall back to
    parsing the text block."""
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return {"error": "client: tool result carried no structured/JSON content", "raw": str(result)}


async def stream_workflow(
    query: str,
    *,
    params_extra: dict[str, Any] | None = None,
    timeout_minutes: float = 20.0,
) -> StreamRun:
    """Drive ``run_workflow_streaming`` over a REAL stdio MCP round-trip and return
    the collected per-stage notifications (arrival order) plus the final result.

    The progress + logging callbacks append to a single ``events`` list so the two
    channels are interleaved in true arrival order. RELIABILITY: each callback body
    is guarded — a handler bug records itself in ``handler_errors`` and is swallowed,
    so a client-side failure can never tear down the session or perturb the server
    run (the server already guarantees its half; this guarantees the client's)."""
    run = StreamRun()
    seq = 0

    def _next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq

    async def logging_callback(params: LoggingMessageNotificationParams) -> None:
        try:
            data = params.data
            stage = data.get("stage") if isinstance(data, dict) else None
            run.events.append(
                StreamEvent(
                    seq=_next_seq(),
                    kind="log",
                    stage=stage,
                    payload=data if isinstance(data, dict) else {"data": data},
                )
            )
        except Exception as exc:  # noqa: BLE001 — a client handler bug must not break the session
            run.handler_errors.append(f"logging_callback: {type(exc).__name__}: {exc}")

    async def progress_callback(progress: float, total: float | None, message: str | None) -> None:
        try:
            stage = message.split("stage complete:", 1)[-1].strip() if message else None
            run.events.append(
                StreamEvent(
                    seq=_next_seq(),
                    kind="progress",
                    stage=stage,
                    payload={"progress": progress, "total": total, "message": message},
                )
            )
        except Exception as exc:  # noqa: BLE001 — same reliability guarantee as above
            run.handler_errors.append(f"progress_callback: {type(exc).__name__}: {exc}")

    params: dict[str, Any] = {"query": query, **(params_extra or {})}
    async with (
        stdio_client(_server_params()) as (read, write),
        ClientSession(read, write, logging_callback=logging_callback) as session,
    ):
        await session.initialize()
        # NOTE (verified E3-5): we deliberately do NOT call
        # ``session.set_logging_level("info")`` here. The apecx FastMCP server
        # does not register the MCP ``logging`` capability, so ``logging/setLevel``
        # is rejected with "Method not found" — which aborts the whole session.
        # The server's ``send_log_message`` does NOT gate on a configured level
        # (mcp/server/session.py just emits the notification), so stage logs are
        # delivered to ``logging_callback`` regardless. A desktop client MUST NOT
        # call ``set_logging_level`` against this server. See
        # docs/desktop_streaming_contract.md.
        result = await session.call_tool(
            "run_workflow_streaming",
            {"name": WORKFLOW_NAME, "params": params},
            progress_callback=progress_callback,
            read_timeout_seconds=timedelta(minutes=timeout_minutes),
        )
    run.result = _result_to_dict(result)
    return run


def _print_run(run: StreamRun) -> None:
    print("=" * 72)
    print(f"ARRIVAL-ORDERED NOTIFICATIONS ({len(run.events)} total)")
    print("=" * 72)
    for e in run.events:
        if e.kind == "progress":
            p = e.payload
            print(f"[{e.seq:>2}] PROGRESS  n={p['progress']!r:<5} stage={e.stage!r}")
        else:
            md = (e.payload.get("markdown") or "").replace("\n", " ")
            order = e.payload.get("order")
            print(f"[{e.seq:>2}] LOG/stage order={order!r:<4} stage={e.stage!r}")
            print(f"        markdown: {md[:160]}")
    print("-" * 72)
    print(f"stages (log channel, arrival order): {[r['stage'] for r in run.stage_reports]}")
    if run.handler_errors:
        print(f"client handler errors (swallowed): {run.handler_errors}")
    result = run.result or {}
    print(f"final status: {result.get('status')!r}  run_id: {result.get('run_id')!r}")
    md = result.get("markdown") or ""
    print(f"final markdown length: {len(md)} chars")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    query = argv[0]
    run = asyncio.run(stream_workflow(query))
    _print_run(run)
    return 0 if (run.result or {}).get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
