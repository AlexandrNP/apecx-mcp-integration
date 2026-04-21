"""Tests for the MCP surface's Control Plane HTTP client.

These exercise the client against a real ASGI app (no mock HTTP layer) using
``httpx.ASGITransport``. We verify:
1. The client talks to the stub routes and gets `NotImplementedError` on 501.
2. Request envelopes are serialized correctly (if they weren't, the server
   would return 422 and the client would raise HTTPStatusError, not NotImplementedError).
3. The healthz round-trip works.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.schemas.api import (
    ApproveRequest,
    CreateApprovalRequest,
    GetStatusRequest,
    ListRunsRequest,
    StartWorkflowRequest,
)
from apecx_integration.control_plane.schemas.enums import ApprovalKind
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


async def test_start_workflow_raises_not_implemented(client: ControlPlaneClient) -> None:
    body = StartWorkflowRequest(description="Run VIOLIN × BV-BRC", user_id="alex")
    with pytest.raises(NotImplementedError) as exc:
        await client.start_workflow(body)
    assert "T09" in str(exc.value) or "composer" in str(exc.value).lower()
    await client.close()


async def test_create_approval_raises_not_implemented(client: ControlPlaneClient) -> None:
    body = CreateApprovalRequest(
        run_id=uuid4(),
        step_id=uuid4(),
        kind=ApprovalKind.SOFT,
        summary="Please review proposed synonyms for EEEV",
    )
    with pytest.raises(NotImplementedError) as exc:
        await client.create_approval(body)
    assert "T10" in str(exc.value) or "T09" in str(exc.value)
    await client.close()


async def test_approve_raises_not_implemented(client: ControlPlaneClient) -> None:
    with pytest.raises(NotImplementedError):
        await client.approve(ApproveRequest(approval_id=uuid4()))
    await client.close()


async def test_list_runs_raises_not_implemented(client: ControlPlaneClient) -> None:
    with pytest.raises(NotImplementedError):
        await client.list_runs(ListRunsRequest(user_id="alex"))
    await client.close()


async def test_get_status_raises_not_implemented(client: ControlPlaneClient) -> None:
    with pytest.raises(NotImplementedError):
        await client.get_status(GetStatusRequest(run_id=uuid4()))
    await client.close()
