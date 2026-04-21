"""TX1 integration: the MCP Control-Plane client against a live ASGI app.

The unit suite (tests/unit/test_control_plane_client.py) exercises envelope
serialization and the 501 → NotImplementedError translation for routes
that remain stubs. This file covers the newly-real endpoints end-to-end:
the client is wired to a fresh ``create_app(engine=cp_engine)`` via
``httpx.ASGITransport``, and each test walks through the round-trip
(request envelope → live handler → real persistence → response envelope).

No mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.schemas.api import (
    ApproveRequest,
    CorrectRequest,
    CreateApprovalRequest,
    GetArtifactRequest,
    GetStatusRequest,
    ListPendingApprovalsRequest,
    ListRunsRequest,
    RejectRequest,
)
from apecx_integration.control_plane.schemas.enums import (
    ApprovalKind,
    ApprovalStatus,
    RunStatus,
)
from apecx_integration.mcp_surface.control_plane_client import ControlPlaneClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


@pytest.fixture
async def cp_client_http(cp_engine) -> ControlPlaneClient:
    cp = ControlPlaneClient("http://testserver")
    cp._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(engine=cp_engine)),
        base_url="http://testserver",
    )
    yield cp
    await cp.close()


def _seed_run_and_step(engine: Engine, *, user_id: str = "alex") -> tuple[UUID, UUID]:
    run_id = uuid4()
    step_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, 'PENDING', :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, status, "
                "input_artifact_ids, output_artifact_ids) "
                "VALUES (:id, :rid, 's', 'LOCAL', 'PAUSED_FOR_APPROVAL', '[]', '[]')"
            ),
            {"id": str(step_id), "rid": str(run_id)},
        )
    return run_id, step_id


async def test_create_approval_round_trips_via_client(
    cp_client_http: ControlPlaneClient, cp_engine
) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    resp = await cp_client_http.create_approval(
        CreateApprovalRequest(
            run_id=run_id,
            step_id=step_id,
            kind=ApprovalKind.HARD,
            summary="review synonyms",
        )
    )
    assert resp.approval.step_id == step_id
    assert resp.approval.status is ApprovalStatus.PENDING
    assert resp.approval.policy["summary"] == "review synonyms"


async def test_approve_reject_correct_via_client(
    cp_client_http: ControlPlaneClient, cp_engine
) -> None:
    run_a, step_a = _seed_run_and_step(cp_engine, user_id="alex")
    run_b, step_b = _seed_run_and_step(cp_engine, user_id="alex")
    run_c, step_c = _seed_run_and_step(cp_engine, user_id="alex")

    for rid, sid in [(run_a, step_a), (run_b, step_b), (run_c, step_c)]:
        await cp_client_http.create_approval(
            CreateApprovalRequest(run_id=rid, step_id=sid, kind=ApprovalKind.HARD, summary="s")
        )

    pending = await cp_client_http.list_pending_approvals(
        ListPendingApprovalsRequest(user_id="alex")
    )
    assert len(pending.approvals) == 3
    ids = [a.id for a in pending.approvals]

    r1 = await cp_client_http.approve(ApproveRequest(approval_id=ids[0], comment="ok"))
    assert r1.approval.status is ApprovalStatus.APPROVED
    assert r1.approval.comment == "ok"

    r2 = await cp_client_http.reject(RejectRequest(approval_id=ids[1], reason="wrong pathogen"))
    assert r2.approval.status is ApprovalStatus.REJECTED

    r3 = await cp_client_http.correct(
        CorrectRequest(approval_id=ids[2], modifications={"synonyms": ["A"]})
    )
    assert r3.approval.status is ApprovalStatus.APPROVED_WITH_MODIFICATIONS
    assert r3.approval.policy["modifications"] == {"synonyms": ["A"]}

    pending_after = await cp_client_http.list_pending_approvals(
        ListPendingApprovalsRequest(user_id="alex")
    )
    assert pending_after.approvals == []


async def test_double_approve_surfaces_http_409(
    cp_client_http: ControlPlaneClient, cp_engine
) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    created = await cp_client_http.create_approval(
        CreateApprovalRequest(run_id=run_id, step_id=step_id, kind=ApprovalKind.HARD, summary="s")
    )
    await cp_client_http.approve(ApproveRequest(approval_id=created.approval.id))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await cp_client_http.approve(ApproveRequest(approval_id=created.approval.id))
    assert exc.value.response.status_code == 409


async def test_unknown_approval_surfaces_http_404(
    cp_client_http: ControlPlaneClient,
) -> None:
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await cp_client_http.approve(ApproveRequest(approval_id=uuid4()))
    assert exc.value.response.status_code == 404


async def test_list_runs_via_client(cp_client_http: ControlPlaneClient, cp_engine) -> None:
    run_id, _ = _seed_run_and_step(cp_engine, user_id="alex")
    _seed_run_and_step(cp_engine, user_id="bob")

    resp = await cp_client_http.list_runs(ListRunsRequest(user_id="alex"))
    assert {r.id for r in resp.runs} == {run_id}

    # Status filter honored.
    resp_filtered = await cp_client_http.list_runs(
        ListRunsRequest(user_id="alex", status_filter=RunStatus.COMPLETED)
    )
    assert resp_filtered.runs == []


async def test_get_status_via_client_includes_pending_approval(
    cp_client_http: ControlPlaneClient, cp_engine
) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    await cp_client_http.create_approval(
        CreateApprovalRequest(run_id=run_id, step_id=step_id, kind=ApprovalKind.HARD, summary="s")
    )
    status = await cp_client_http.get_status(GetStatusRequest(run_id=run_id))
    assert status.run.id == run_id
    assert len(status.steps) == 1
    assert status.pending_approval is not None
    assert status.pending_approval.status is ApprovalStatus.PENDING


async def test_get_artifact_via_client_reports_t11_gap(
    cp_client_http: ControlPlaneClient, cp_engine
) -> None:
    run_id, _ = _seed_run_and_step(cp_engine)
    artifact_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, content_hash, "
                "size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'INPUT', '/tmp/a', :h, 0, 'text/plain', :ts)"
            ),
            {
                "id": str(artifact_id),
                "rid": str(run_id),
                "h": "b" * 64,
                "ts": datetime.now(UTC).isoformat(),
            },
        )
    resp = await cp_client_http.get_artifact(GetArtifactRequest(artifact_id=artifact_id))
    assert resp.artifact.id == artifact_id
    assert resp.inline_bytes is None
    assert resp.reason_inline_omitted is not None and "T11" in resp.reason_inline_omitted


async def test_still_stubbed_start_workflow_raises_not_implemented(
    cp_client_http: ControlPlaneClient,
) -> None:
    from apecx_integration.control_plane.schemas.api import StartWorkflowRequest

    with pytest.raises(NotImplementedError) as exc:
        await cp_client_http.start_workflow(StartWorkflowRequest(description="x", user_id="alex"))
    # 501 detail points at composer or T09.
    assert "composer" in str(exc.value).lower() or "T09" in str(exc.value)
