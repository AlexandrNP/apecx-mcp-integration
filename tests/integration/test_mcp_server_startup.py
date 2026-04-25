"""MCP server startup-health-check tests.

Audit §3.2 (docs/codebase_audit_2026_04_24.md). Pre-fix the lazy
``get_client()`` meant a misconfigured ``APECX_CONTROL_PLANE_URL``
only surfaced when a scientist invoked a tool. After the fix,
``server.main()`` calls ``_verify_control_plane_reachable()`` which
hits ``/healthz`` synchronously and exits with code 2 if unreachable
(unless ``APECX_MCP_SKIP_HEALTHCHECK=1``).
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
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.server import (
        _verify_control_plane_reachable,
    )

    class _StubHealthz:
        def __init__(self):
            self.calls = 0

        async def __call__(self):
            self.calls += 1
            return {"status": "ok"}

    client = ControlPlaneClient("http://stub.invalid")
    stub = _StubHealthz()
    client.healthz = stub  # type: ignore[method-assign]
    _shared.set_client(client)

    asyncio.run(_verify_control_plane_reachable())
    assert stub.calls == 1


def test_verify_control_plane_reachable_exits_when_healthz_unreachable(
    monkeypatch,
):
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    from apecx_integration.mcp_surface.server import (
        _verify_control_plane_reachable,
    )

    async def _raises():
        raise httpx.ConnectError("CP unreachable in test")

    client = ControlPlaneClient("http://unreachable.invalid")
    client.healthz = _raises  # type: ignore[method-assign]
    _shared.set_client(client)

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

    async def _raises():
        raise AssertionError("healthz should NOT be called when skip-flag set")

    client = ControlPlaneClient("http://unreachable.invalid")
    client.healthz = _raises  # type: ignore[method-assign]
    _shared.set_client(client)

    monkeypatch.setenv("APECX_MCP_SKIP_HEALTHCHECK", "1")
    asyncio.run(_verify_control_plane_reachable())
