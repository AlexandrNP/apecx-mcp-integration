"""Tests for the MCP surface's Control Plane HTTP client.

Exercise the client against a real ASGI app (no mock HTTP layer)
using ``httpx.ASGITransport``. We verify:
1. Healthz round-trip.
2. The client surfaces a 503 "not configured" error as
   ``ControlPlaneDependencyError`` when ``create_app()`` is built
   without a composer. Integration tests cover the 200 happy path
   with a wired composer.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.schemas.api import (
    ExecuteWorkflowRequest,
    StartWorkflowRequest,
)
from apecx_integration.mcp_surface.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneDependencyError,
    ControlPlaneNotFoundError,
    WorkflowCompositionError,
)


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


async def test_start_workflow_503_wraps_as_dependency_error(
    client: ControlPlaneClient,
) -> None:
    """Audit §3.3. Pre-fix the 503 fell through ``raise_for_status()``
    and surfaced as a raw ``httpx.HTTPStatusError`` (exposing httpx
    internals to MCP callers). Now wrapped in
    ``ControlPlaneDependencyError`` for parity with the 501 →
    ``NotImplementedError`` path; the original 503 detail message
    is preserved as the exception's str.
    """
    body = StartWorkflowRequest(description="Run VIOLIN × BV-BRC", user_id="alex")
    with pytest.raises(ControlPlaneDependencyError) as exc:
        await client.start_workflow(body)
    assert "Composer is not configured" in str(exc.value)
    await client.close()


async def test_422_raises_workflow_composition_error_with_detail() -> None:
    """A 422 from the Control Plane (known composition failure) is wrapped
    in ``WorkflowCompositionError`` carrying the server ``detail`` so an MCP
    caller sees WHY instead of an opaque 500 / raw httpx traceback.

    Uses ``httpx.MockTransport`` (an httpx-native fake response, not a mock
    of the composer) to drive the client's 422 branch directly. The real
    422-producing route path is covered by
    ``tests/integration/test_api_workflow_errors.py``.
    """
    detail = (
        "workflow composition failed: spec mode: expander could not "
        "realize the spec: step 'x': class_name 'RagDomainSearchOnly' "
        "has no catalog match."
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": detail})

    cp = ControlPlaneClient("http://testserver")
    cp._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://testserver",
    )
    body = StartWorkflowRequest(description="x", user_id="alex")
    with pytest.raises(WorkflowCompositionError) as exc:
        await cp.start_workflow(body)
    assert detail in str(exc.value)
    assert "has no catalog match" in str(exc.value)
    await cp.close()


async def test_404_raises_run_not_found_error_with_detail() -> None:
    """A 404 from the Control Plane (``/workflows/execute`` on a non-existent
    run) is wrapped in ``ControlPlaneNotFoundError`` carrying the server ``detail`` so an
    MCP caller sees "no such run" instead of an opaque httpx traceback.

    Uses ``httpx.MockTransport`` (an httpx-native fake response, not a mock of
    the executor) to drive the client's 404 status-dispatch branch directly.
    The real 404-producing route path is covered by
    ``tests/integration/test_api_workflow_errors.py::test_execute_nonexistent_run_returns_404``.
    """
    run_id = uuid4()
    detail = f"run {run_id} not found; cannot execute a non-existent run."

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": detail})

    cp = ControlPlaneClient("http://testserver")
    cp._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://testserver",
    )
    body = ExecuteWorkflowRequest(run_id=run_id)
    with pytest.raises(ControlPlaneNotFoundError) as exc:
        await cp.execute_workflow(body)
    assert detail in str(exc.value)
    assert "not found" in str(exc.value)
    await cp.close()
