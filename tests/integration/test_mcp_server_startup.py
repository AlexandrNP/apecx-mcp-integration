"""MCP server startup-health-check tests.

From the 2026-04-24 codebase audit §3.2. Pre-fix the lazy
``get_client()`` meant a misconfigured ``APECX_CONTROL_PLANE_URL``
only surfaced when a scientist invoked a tool. After the fix,
``server.main()`` calls ``_verify_control_plane_reachable()`` which
hits ``/healthz`` synchronously and exits with code 2 if unreachable
(unless ``APECX_MCP_SKIP_HEALTHCHECK=1``).

**2026-04-25 update:** the original implementation called
``get_client()`` for the health check, which lazy-built the global
singleton inside ``main()``'s ``asyncio.run()`` loop. That loop
closed when the health check returned, leaving the singleton bound
to a dead loop. Every subsequent FastMCP tool call then failed with
"Event loop is closed". The fix builds an EPHEMERAL
``ControlPlaneClient`` for the health check and closes it before
returning. The tool-call singleton (``_shared._client``) is now
guaranteed not to be touched by startup. The
``test_health_check_does_not_pollute_singleton`` test below pins
that contract.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from apecx_integration.mcp_surface.tools import _shared


@pytest.fixture(autouse=True)
def reset_shared_client():
    """Each test gets a clean global client slot."""
    _shared.set_client(None)
    yield
    _shared.set_client(None)


def test_verify_control_plane_reachable_passes_when_healthz_returns_200(
    monkeypatch,
):
    """Patch the ControlPlaneClient's healthz to succeed; assert
    ``_verify_control_plane_reachable`` returns cleanly. The 2026-04-25
    fix builds its OWN ControlPlaneClient via the constructor, so we
    monkey-patch the class method instead of injecting via
    ``_shared.set_client``.
    """
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.server import (
        _verify_control_plane_reachable,
    )

    calls = {"count": 0}

    async def _stub_healthz(self):  # noqa: ARG001
        calls["count"] += 1
        return {"status": "ok"}

    monkeypatch.setattr(ControlPlaneClient, "healthz", _stub_healthz)
    asyncio.run(_verify_control_plane_reachable())
    assert calls["count"] == 1


def test_verify_control_plane_reachable_exits_when_healthz_unreachable(
    monkeypatch,
):
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.server import (
        _verify_control_plane_reachable,
    )

    async def _raises(self):  # noqa: ARG001
        raise httpx.ConnectError("CP unreachable in test")

    monkeypatch.setattr(ControlPlaneClient, "healthz", _raises)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(_verify_control_plane_reachable())
    assert excinfo.value.code == 2


def test_verify_control_plane_skipped_when_env_var_set(monkeypatch):
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.server import (
        _verify_control_plane_reachable,
    )

    async def _raises(self):  # noqa: ARG001
        raise AssertionError("healthz should NOT be called when skip-flag set")

    monkeypatch.setattr(ControlPlaneClient, "healthz", _raises)
    monkeypatch.setenv("APECX_MCP_SKIP_HEALTHCHECK", "1")
    asyncio.run(_verify_control_plane_reachable())


def test_health_check_does_not_pollute_singleton(monkeypatch):
    """Regression guard for the 2026-04-25 "Event loop is closed"
    bug found via stdio JSON-RPC e2e probe.

    Pre-fix sequence:
      1. main() -> asyncio.run(_verify_control_plane_reachable())
      2. _verify... calls get_client() which lazy-builds the
         singleton ControlPlaneClient (containing an
         httpx.AsyncClient bound to asyncio.run's loop).
      3. asyncio.run() closes its loop on return.
      4. FastMCP starts a new event loop in server.run().
      5. tool call -> get_client() returns the same singleton
         -> AsyncClient.post() -> "Event loop is closed".

    Post-fix: ``_verify_control_plane_reachable`` builds an
    EPHEMERAL ControlPlaneClient and closes it before returning;
    the singleton (``_shared._client``) is left untouched, so the
    first tool call inside FastMCP's loop builds a fresh client
    bound to the right loop.

    This test asserts that after a successful health check, the
    global ``_shared._client`` is still ``None``.
    """
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.server import (
        _verify_control_plane_reachable,
    )

    async def _stub(self):  # noqa: ARG001
        return {"status": "ok"}

    monkeypatch.setattr(ControlPlaneClient, "healthz", _stub)
    # Confirm starting state.
    assert _shared._client is None

    asyncio.run(_verify_control_plane_reachable())

    # The post-fix contract: singleton is still uninitialized.
    assert _shared._client is None, (
        "Health check polluted the get_client() singleton; tool calls "
        "would later fail with 'Event loop is closed' because the "
        "AsyncClient is bound to asyncio.run's now-closed loop."
    )
