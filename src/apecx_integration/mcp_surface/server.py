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

Backend autostart (added 2026-04-27 — config-only-deployment UX)
----------------------------------------------------------------
When ``APECX_MCP_AUTOSTART_BACKEND=1`` (the recommended default for
Claude Desktop installs) and the Control Plane is unreachable at
startup, this module spawns ``apecx-cp serve`` as a child process,
polls ``/healthz`` until it answers, and registers an atexit hook
to terminate the child on MCP-server exit. The child uses SQLite
for state by default — no Docker / Postgres required for the
single-user case the MCP target audience runs.

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
import atexit
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from apecx_integration.mcp_surface.tools import (
    approvals as approvals_tools,
)
from apecx_integration.mcp_surface.tools import (
    database_tools,
)
from apecx_integration.mcp_surface.tools import (
    discovery as discovery_tools,
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


_CHILD_BACKEND_PROC: subprocess.Popen[bytes] | None = None
_CHILD_BACKEND_LOG: Path | None = None


def build_server() -> FastMCP:
    """Construct the FastMCP server with every tool registered.

    Split from ``main()`` so tests can exercise the server without
    launching the stdio transport.
    """
    server: FastMCP = FastMCP("apecx-mcp")

    server.tool()(workflow_tools.start_workflow)
    server.tool()(workflow_tools.show_diff)
    server.tool()(workflow_tools.execute_workflow)

    # Discovery — read-only catalog tools that let the model see
    # which workflows / components the composer can actually build
    # before it calls start_workflow.
    server.tool()(discovery_tools.list_workflows)
    server.tool()(discovery_tools.describe_workflow)

    # Direct database lookups — bypass the composer for one-shot
    # VIOLIN + BV-BRC queries the model can answer without orchestrating
    # a workflow ("list vaccines targeting EEEV").
    server.tool()(database_tools.query_vaccines)
    server.tool()(database_tools.query_pathogens)
    server.tool()(database_tools.query_genes)
    server.tool()(database_tools.query_bvbrc_genomes)
    server.tool()(database_tools.get_vaccine_pathogen_genes)
    server.tool()(database_tools.resolve_entity)
    server.tool()(database_tools.database_statistics)

    server.tool()(approvals_tools.list_pending_approvals)
    server.tool()(approvals_tools.approve)
    server.tool()(approvals_tools.reject)
    server.tool()(approvals_tools.correct)

    server.tool()(hpc_tools.estimate_cost)
    server.tool()(hpc_tools.confirm_allocation)
    server.tool()(hpc_tools.export_hpc_bundle)
    server.tool()(hpc_tools.ingest_hpc_bundle)

    return server


async def _ping_control_plane(base_url: str) -> bool:
    """Hit /healthz — return True iff the backend answers."""
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    ephemeral = ControlPlaneClient(base_url)
    try:
        await ephemeral.healthz()
        return True
    except Exception:  # noqa: BLE001 — any wire error is "unreachable"
        return False
    finally:
        await ephemeral.close()


def _autostart_backend(base_url: str) -> subprocess.Popen[bytes] | None:
    """Spawn ``apecx-cp serve`` as a child process.

    Returns the Popen handle on success; None on failure (in which
    case the caller should fall back to the eager-fail path).

    Lifecycle: child is registered via atexit so it terminates when
    the MCP server exits. Stderr lands in a log file under
    ``$TMPDIR/apecx-cp-autostart.log`` so the operator can debug
    backend issues without losing the stdout MCP stream.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8000

    # Refuse to autostart against a non-loopback host. Spawning a
    # backend bound to 0.0.0.0 / a public IP from inside an MCP
    # client is a significant security shape — the operator should
    # do that explicitly.
    if host not in {"127.0.0.1", "localhost", "::1"}:
        log.error(
            "MCP autostart: refusing to spawn backend for "
            "non-loopback URL %s. Configure APECX_CONTROL_PLANE_URL "
            "to a loopback address, or start the backend manually "
            "and set APECX_MCP_AUTOSTART_BACKEND=0.",
            base_url,
        )
        return None

    # Find the apecx-cp entry point. When this module is installed
    # editable into a venv, the binary lives next to ``apecx-mcp`` in
    # the same bin dir. ``shutil.which`` finds it on PATH (which
    # FastMCP's spawned context inherits).
    cp_binary = shutil.which("apecx-cp")
    if cp_binary is None:
        # Fall back to the same Python interpreter + module form. This
        # works when the venv's bin dir isn't on PATH (rare for
        # editable installs but happens with isolated launch contexts).
        cp_binary = sys.executable
        cp_args = ["-m", "apecx_integration.control_plane.app", "serve",
                   "--host", host, "--port", str(port)]
    else:
        cp_args = ["serve", "--host", host, "--port", str(port)]

    import tempfile
    log_path = Path(tempfile.gettempdir()) / "apecx-cp-autostart.log"
    log_fh = log_path.open("ab")
    log.info(
        "MCP autostart: spawning backend (%s %s) — child stderr -> %s",
        cp_binary, " ".join(cp_args), log_path,
    )

    # Inherit env (LLM vars + APECX_CP_POSTGRES_URL if set). Force
    # SQLite default by NOT setting POSTGRES URL when absent.
    proc = subprocess.Popen(
        [cp_binary, *cp_args],
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        # Detach from MCP server's process group so child doesn't
        # receive the SIGINT MCP server gets on Ctrl-C from Claude
        # Desktop. We terminate it explicitly via atexit.
        start_new_session=True,
    )

    global _CHILD_BACKEND_PROC, _CHILD_BACKEND_LOG
    _CHILD_BACKEND_PROC = proc
    _CHILD_BACKEND_LOG = log_path
    atexit.register(_terminate_child_backend)
    return proc


def _terminate_child_backend() -> None:
    """atexit handler — SIGTERM the child backend with a 5s grace,
    then SIGKILL if it lingers."""
    proc = _CHILD_BACKEND_PROC
    if proc is None or proc.poll() is not None:
        return  # already exited
    log.info("MCP shutdown: terminating autostart backend (pid=%s)", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        log.warning(
            "MCP shutdown: backend did not terminate within 5s; "
            "sending SIGKILL"
        )
        proc.kill()
        proc.wait(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("MCP shutdown: error during backend cleanup: %s", exc)


async def _wait_for_backend_ready(
    base_url: str, *, timeout_s: float = 60.0,
) -> bool:
    """Poll /healthz until it answers or timeout. Returns True on
    success, False on timeout. 60s default covers SQLite migrations
    on a cold first run."""
    deadline = time.monotonic() + timeout_s
    poll_interval = 0.5
    while time.monotonic() < deadline:
        if await _ping_control_plane(base_url):
            return True
        # If the child died early, fail fast — no point polling.
        if _CHILD_BACKEND_PROC and _CHILD_BACKEND_PROC.poll() is not None:
            return False
        await asyncio.sleep(poll_interval)
    return False


async def _verify_control_plane_reachable() -> None:
    """Ensure the Control Plane is reachable, autostarting it if
    configured to do so.

    Behavior matrix:

      ============================  ===============  ============================
      APECX_MCP_SKIP_HEALTHCHECK    Backend up        Action
      ============================  ===============  ============================
      1                             —                 Skip; warn-log only
      0 (default)                   yes               Log success
      0 (default)                   no, autostart=1   Spawn backend; poll; succeed
                                                      OR exit(2) on poll timeout
      0 (default)                   no, autostart=0   Exit(2) with the
                                                      ``set APECX_CONTROL_PLANE_URL``
                                                      remediation hint
      ============================  ===============  ============================

    Audit §3.2: pre-fix the lazy client meant a misconfigured URL
    only surfaced when a scientist actually invoked a tool. Eager-
    failing at startup gives the operator an immediate, actionable
    error.
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

    if await _ping_control_plane(base_url):
        log.info("MCP startup: Control Plane at %s reachable.", base_url)
        return

    autostart_enabled = os.environ.get(
        "APECX_MCP_AUTOSTART_BACKEND", "1"
    ) != "0"
    if not autostart_enabled:
        log.error(
            "MCP startup: Control Plane at %s is unreachable AND "
            "APECX_MCP_AUTOSTART_BACKEND=0. Either start the "
            "backend manually (`apecx-cp serve`), set "
            "APECX_MCP_AUTOSTART_BACKEND=1, or set "
            "APECX_MCP_SKIP_HEALTHCHECK=1 to bypass the check.",
            base_url,
        )
        raise SystemExit(2)

    log.info(
        "MCP startup: Control Plane at %s unreachable; autostarting "
        "backend (set APECX_MCP_AUTOSTART_BACKEND=0 to disable).",
        base_url,
    )
    proc = _autostart_backend(base_url)
    if proc is None:
        log.error(
            "MCP startup: autostart failed — could not locate the "
            "apecx-cp binary or refused to spawn. See the autostart "
            "log path printed above; verify ``apecx-cp`` is on PATH "
            "or in the same venv as ``apecx-mcp``."
        )
        raise SystemExit(2)

    if not await _wait_for_backend_ready(base_url, timeout_s=60.0):
        # Surface the child's stderr tail so the operator sees what
        # actually went wrong (port conflict, missing dep, etc.).
        log.error(
            "MCP startup: autostart spawned backend (pid=%s) but it "
            "did not become ready within 60s. Backend log: %s",
            proc.pid, _CHILD_BACKEND_LOG,
        )
        if _CHILD_BACKEND_LOG and _CHILD_BACKEND_LOG.is_file():
            try:
                tail = _CHILD_BACKEND_LOG.read_text(
                    encoding="utf-8", errors="ignore",
                )[-2000:]
                log.error("Backend log tail:\n%s", tail)
            except Exception:  # noqa: BLE001
                pass
        raise SystemExit(2)

    log.info(
        "MCP startup: autostart succeeded — backend at %s, child pid=%s",
        base_url, proc.pid,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(_verify_control_plane_reachable())
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
