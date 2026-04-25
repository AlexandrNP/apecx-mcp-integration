"""MCP surface entry point — FastMCP-based server.

Registers the tool modules and runs the stdio transport so Claude
Desktop (or any MCP client) can invoke them. Reads
``APECX_CONTROL_PLANE_URL`` (default ``http://localhost:8000``)
to decide where the backend lives.

Running
-------
    python -m apecx_integration.mcp_surface.server

The server speaks MCP over stdio; Claude Desktop's client wires it
automatically when configured.

What's exposed
--------------
- ``start_workflow`` / ``show_diff`` / ``execute_workflow``
- ``list_pending_approvals`` / ``approve`` / ``reject`` / ``correct``
- ``estimate_cost`` / ``confirm_allocation``
- ``export_hpc_bundle`` / ``ingest_hpc_bundle``

What's deliberately NOT exposed
-------------------------------
- ``/hpc/submit`` — still 501 at the Control Plane (needs T04/T05
  executor runtime). A tool that always errors is strictly worse
  than "tool absent."
- ``create_approval`` — internal, called by nanobrain's
  ApprovalStep during workflow execution, not a scientist-facing
  tool.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from apecx_integration.mcp_surface.tools import (
    approvals as approvals_tools,
)
from apecx_integration.mcp_surface.tools import (
    hpc as hpc_tools,
)
from apecx_integration.mcp_surface.tools import (
    workflows as workflow_tools,
)
# Note: ``get_client`` is intentionally NOT imported here. The
# startup health check builds an ephemeral client (see
# ``_verify_control_plane_reachable``) so it doesn't pollute the
# tool-call singleton with a dead-loop binding.

log = logging.getLogger(__name__)


def build_server() -> FastMCP:
    """Construct the FastMCP server with every tool registered.

    Split from ``main()`` so tests can exercise the server without
    launching the stdio transport.
    """
    server: FastMCP = FastMCP("apecx-mcp")

    server.tool()(workflow_tools.start_workflow)
    server.tool()(workflow_tools.show_diff)
    server.tool()(workflow_tools.execute_workflow)

    server.tool()(approvals_tools.list_pending_approvals)
    server.tool()(approvals_tools.approve)
    server.tool()(approvals_tools.reject)
    server.tool()(approvals_tools.correct)

    server.tool()(hpc_tools.estimate_cost)
    server.tool()(hpc_tools.confirm_allocation)
    server.tool()(hpc_tools.export_hpc_bundle)
    server.tool()(hpc_tools.ingest_hpc_bundle)

    return server


async def _verify_control_plane_reachable() -> None:
    """Hit /healthz on the configured Control Plane URL.

    Audit §3.2: pre-fix the lazy client meant a misconfigured
    ``APECX_CONTROL_PLANE_URL`` only surfaced when a scientist
    actually invoked a tool, by which point the operator had no
    signal that anything was wrong. Eager-failing at startup gives
    the operator an immediate, actionable error.

    Set ``APECX_MCP_SKIP_HEALTHCHECK=1`` to skip this guard — useful
    for offline development or when the Control Plane is intentionally
    deferred (e.g., during MCP-only smoke testing).
    """
    if os.environ.get("APECX_MCP_SKIP_HEALTHCHECK") == "1":
        log.info(
            "MCP startup: APECX_MCP_SKIP_HEALTHCHECK=1, skipping CP "
            "reachability check."
        )
        return
    base_url = os.environ.get(
        "APECX_CONTROL_PLANE_URL", "http://localhost:8000"
    )
    # Build an EPHEMERAL client; do NOT touch the get_client()
    # singleton here. The singleton's httpx.AsyncClient binds to
    # whatever event loop is current at construction. main() runs
    # this function via asyncio.run(), which closes its loop on
    # return. If we built the singleton inside that loop, every
    # FastMCP tool call later in main() would hit the singleton
    # bound to a dead loop and fail with "Event loop is closed".
    # Discovered via stdio JSON-RPC e2e probe 2026-04-25 — the
    # cluster D §3.2 fix was correct in spirit but introduced this
    # lifecycle regression. Keep startup-time and tool-time clients
    # strictly separate.
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )

    ephemeral = ControlPlaneClient(base_url)
    try:
        resp = await ephemeral.healthz()
    except Exception as exc:  # noqa: BLE001 — must catch any wire error
        log.error(
            "MCP startup: Control Plane at %s is unreachable (%s: %s). "
            "Set APECX_CONTROL_PLANE_URL to the correct URL, or "
            "APECX_MCP_SKIP_HEALTHCHECK=1 to bypass. Tool calls would "
            "fail at first invocation; failing fast at startup instead.",
            base_url,
            type(exc).__name__,
            exc,
        )
        await ephemeral.close()
        raise SystemExit(2) from exc
    await ephemeral.close()
    log.info(
        "MCP startup: Control Plane at %s reachable (%s).", base_url, resp
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(_verify_control_plane_reachable())
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
