"""``infrastructure_status`` MCP tool.

Reports the current health of every backend ``apecx-mcp`` depends on
(Postgres, Redis, MinIO, Ollama, Rhea MCP). The orchestrator runs in
the background; this tool reads its state and re-probes ready
backends so a backend that died mid-session is reported as
``degraded`` immediately (never stale green).

Operator UX
-----------
Claude Desktop renders the dict the tool returns back to the operator
as a fenced JSON block. When something is broken, the
``actionable`` field carries the one-line remedy(ies) — the operator
should be able to act on the message without reading any extra docs.

Failure shape
-------------
The tool itself NEVER raises. If the singleton is somehow missing
(e.g. an import-time crash), the tool returns ``overall: "down"``
with an actionable message and an empty backend list. The MCP wire
contract is "always return a dict the model can read".
"""

from __future__ import annotations

import logging
from typing import Any

from apecx_integration.infrastructure.orchestrator import get_orchestrator

log = logging.getLogger(__name__)


async def infrastructure_status() -> dict[str, Any]:
    """Returns the current health of every backend apecx-mcp depends on.

    Returns a dict with the shape::

        {
            "overall": "ready" | "starting" | "degraded" | "down" | "disabled",
            "autostart_enabled": bool,
            "orchestrator_uptime_seconds": float,
            "start_all_completed": bool,
            "backends": [
                {
                    "name": str,
                    "display_name": str,
                    "kind": "docker_container" | "external",
                    "required": bool,
                    "state": <BackendState string>,
                    "detail": str,
                    "last_probe_at": float (unix timestamp),
                    "latency_ms": float,
                    "error": str (optional),
                    "spawned_by_us": bool,
                    "tags": [str, ...],
                    ...
                },
                ...
            ],
            "actionable": [<list of operator-actionable strings>]
        }

    The status tool ALWAYS re-probes ready backends with a short
    timeout. A backend that has died since startup is reported as
    ``degraded`` — we never return stale green from N minutes ago.
    """
    try:
        orchestrator = get_orchestrator()
        # Self-heal: if Docker was started AFTER the MCP server came up, re-attempt the
        # stuck docker backends before reporting. Cheap when nothing is stuck; the expensive
        # re-attempt is throttled inside reconcile().
        await orchestrator.reconcile()
        snapshot = await orchestrator.status()
        return snapshot
    except Exception as exc:  # noqa: BLE001
        # Last-resort safety net. The orchestrator's own paths catch
        # their own errors and report them as actionable messages —
        # arriving here means we hit a programmer bug (probably an
        # import error in the infrastructure subpackage). Surface
        # what we can.
        log.exception("infrastructure_status: unexpected error reading orchestrator state")
        return {
            "overall": "down",
            "autostart_enabled": False,
            "orchestrator_uptime_seconds": 0.0,
            "start_all_completed": False,
            "backends": [],
            "actionable": [
                f"[orchestrator] internal error: {type(exc).__name__}: {exc}. "
                "Check the MCP server log for the full traceback."
            ],
        }


__all__ = ["infrastructure_status"]
