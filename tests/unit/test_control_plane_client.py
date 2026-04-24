"""Tests for the MCP surface's Control Plane HTTP client.

Exercise the client against a real ASGI app (no mock HTTP layer)
using ``httpx.ASGITransport``. We verify:
1. Healthz round-trip.
2. The client surfaces a 503 "not configured" error when
   ``create_app()`` is built without a composer. Integration tests
   cover the 200 happy path with a wired composer.
"""

from __future__ import annotations

import httpx
import pytest
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.schemas.api import (
    StartWorkflowRequest,
)
from apecx_integration.mcp_surface.control_plane_client import ControlPlaneClient


@pytest.fixture(name="client")
async def _client() -> ControlPlaneClient:
    cp = ControlPlaneClient("http://testserver")
    cp._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
    )
    return cp


async def test_healthz_round_trips(client: ControlPlaneClient) -> None:
    assert await client.healthz() == {"status": "ok", "phase": "scaffold"}
    await client.close()


async def test_start_workflow_surfaces_503_when_composer_absent(
    client: ControlPlaneClient,
) -> None:
    """Bare ``create_app()`` has no composer → /workflows/start
    returns 503. The MCP client must propagate that as an
    HTTPStatusError (previously this test asserted NotImplementedError
    because the route was a 501 stub; the route is real now, just
    unconfigured on this test app)."""
    body = StartWorkflowRequest(description="Run VIOLIN × BV-BRC", user_id="alex")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await client.start_workflow(body)
    assert exc.value.response.status_code == 503
    assert "Composer is not configured" in exc.value.response.json()["detail"]
    await client.close()
