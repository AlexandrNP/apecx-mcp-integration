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


def main() -> None:
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
