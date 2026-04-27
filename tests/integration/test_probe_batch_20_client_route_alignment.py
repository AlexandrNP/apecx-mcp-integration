"""Probe batch 20 — ControlPlaneClient ↔ FastAPI route alignment
(probes 505-529).

After batch 18 found two MCP-tool-to-schema mismatches and batch 19
audited the rest of the MCP layer, this batch goes one tier deeper:
the ControlPlaneClient itself. A "client posts to /hpc/estimate but
route is /hpc/estimate_cost" bug would manifest as 404 Not Found in
production and be caught by no current test.

Approach: use ``httpx.MockTransport`` to intercept every HTTP request
the client makes, capture URL + method + body, and assert it matches
what the route declares. No FastAPI app spin-up needed.

Tools audited (one client method per route):
  /workflows/start, /workflows/diff, /workflows/execute
  /hpc/estimate, /hpc/confirm, /hpc/export, /hpc/ingest
  /approvals/, /approvals/approve, /approvals/reject,
  /approvals/correct, /approvals/pending
  /healthz
  /verified_synonyms/{id} (PATCH)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest


pytestmark = pytest.mark.integration


_VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _make_client(handler):
    """Build a ControlPlaneClient that routes every request through the
    supplied handler (httpx.MockTransport callable)."""
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    transport = httpx.MockTransport(handler)
    client = ControlPlaneClient("http://test.invalid")
    # Replace the internal AsyncClient with one bound to MockTransport
    client._client = httpx.AsyncClient(
        base_url="http://test.invalid", transport=transport
    )
    return client


def _capture_handler(payloads: dict, response_json: dict, status: int = 200):
    """Returns a handler that records the request and replies with
    ``response_json`` at ``status``."""
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = None
        payloads["method"] = request.method
        payloads["url"] = str(request.url.path)
        payloads["body"] = parsed
        return httpx.Response(status, json=response_json)
    return handler


# ---------------------------------------------------------------------------
# Path + method alignment — probes 505-516
# ---------------------------------------------------------------------------


def _approval_response_json(status: str = "approved") -> dict[str, Any]:
    """Mirror the real Approval schema (entities.Approval) — extra='forbid'
    means we have to match its fields exactly."""
    return {
        "approval": {
            "id": _VALID_UUID,
            "step_id": _VALID_UUID,
            "kind": "hard",
            "status": status,
            "policy": {},
            "decided_by": "u" if status != "pending" else None,
            "decided_at": "2026-04-26T00:00:00+00:00" if status != "pending" else None,
            "comment": "" if status != "pending" else None,
        }
    }


def _run_response_json() -> dict[str, Any]:
    return {
        "id": _VALID_UUID,
        "user_id": "u",
        "workflow_config_id": None,
        "status": "running",
        "created_at": "2026-04-26T00:00:00+00:00",
        "started_at": None,
        "completed_at": None,
        "parent_run_id": None,
    }


def test_probe_505_start_workflow_path() -> None:
    from apecx_integration.control_plane.schemas.api import StartWorkflowRequest
    captured: dict = {}
    response = {
        "run": _run_response_json(),
        "generated_workflow_artifact_id": _VALID_UUID,
    }
    client = _make_client(_capture_handler(captured, response))
    asyncio.run(client.start_workflow(StartWorkflowRequest(
        description="x", user_id="u",
    )))
    assert captured["method"] == "POST"
    assert captured["url"] == "/workflows/start"


def test_probe_506_show_yaml_diff_path() -> None:
    from apecx_integration.control_plane.schemas.api import ShowYamlDiffRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {
        "yaml_text": "name: x",
        "novel_python_by_step": {},
        "categorization": [],
        "summary_sentence": "",
    }))
    asyncio.run(client.show_yaml_diff(
        ShowYamlDiffRequest(run_id=uuid.UUID(_VALID_UUID))
    ))
    assert captured["url"] == "/workflows/diff"


def test_probe_507_execute_workflow_path() -> None:
    from apecx_integration.control_plane.schemas.api import ExecuteWorkflowRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {
        "run_id": _VALID_UUID,
        "status": "completed",
        "output_artifact_id": _VALID_UUID,
        "reason": None,
    }))
    asyncio.run(client.execute_workflow(
        ExecuteWorkflowRequest(run_id=uuid.UUID(_VALID_UUID))
    ))
    assert captured["url"] == "/workflows/execute"


def test_probe_508_estimate_cost_path() -> None:
    from apecx_integration.control_plane.schemas.api import EstimateCostRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {
        "total_core_hours": 0.0, "per_step_core_hours": {},
        "confidence_interval": [0.0, 0.0], "endpoint": "local",
    }))
    asyncio.run(client.estimate_cost(
        EstimateCostRequest(run_id=uuid.UUID(_VALID_UUID))
    ))
    assert captured["url"] == "/hpc/estimate"


def test_probe_509_confirm_allocation_path() -> None:
    from apecx_integration.control_plane.schemas.api import (
        ConfirmAllocationRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {
        "run_id": _VALID_UUID,
        "confirmed": True,
    }))
    asyncio.run(client.confirm_allocation(
        ConfirmAllocationRequest(
            run_id=uuid.UUID(_VALID_UUID), confirmed_core_hours=1.0
        )
    ))
    assert captured["url"] == "/hpc/confirm"


def test_probe_510_export_hpc_bundle_path() -> None:
    from apecx_integration.control_plane.schemas.api import (
        ExportHpcBundleRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {
        "bundle_path": "/tmp/b", "submit_command": "qsub",
    }))
    asyncio.run(client.export_hpc_bundle(
        ExportHpcBundleRequest(
            run_id=uuid.UUID(_VALID_UUID),
            target_system="polaris",
            output_directory="/tmp",
        )
    ))
    assert captured["url"] == "/hpc/export"


def test_probe_511_ingest_hpc_bundle_path() -> None:
    from apecx_integration.control_plane.schemas.api import (
        IngestHpcBundleRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {
        "run_id": _VALID_UUID, "status": "completed",
        "output_artifact_id": None,
    }))
    asyncio.run(client.ingest_hpc_bundle(
        IngestHpcBundleRequest(bundle_path="/tmp/b")
    ))
    assert captured["url"] == "/hpc/ingest"


def test_probe_512_create_approval_path() -> None:
    """The internal nanobrain ApprovalStep posts here. POST to
    /approvals/ — trailing slash matters; FastAPI distinguishes
    /approvals from /approvals/ for prefix routing."""
    from apecx_integration.control_plane.schemas.api import (
        CreateApprovalRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(
        captured, _approval_response_json(status="pending")
    ))
    asyncio.run(client.create_approval(
        CreateApprovalRequest(
            run_id=uuid.UUID(_VALID_UUID),
            step_id=uuid.UUID(_VALID_UUID),
            kind="hard",
            summary="x",
        )
    ))
    assert captured["url"] == "/approvals/"


def test_probe_513_approve_path() -> None:
    from apecx_integration.control_plane.schemas.api import ApproveRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, _approval_response_json()))
    asyncio.run(client.approve(ApproveRequest(
        approval_id=uuid.UUID(_VALID_UUID),
        decided_by="u",
        comment="ok",
    )))
    assert captured["url"] == "/approvals/approve"


def test_probe_514_reject_path() -> None:
    """Cluster AO regression — confirms reject's body has 'reason'
    not 'comment' AND the path is /approvals/reject."""
    from apecx_integration.control_plane.schemas.api import RejectRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, _approval_response_json()))
    asyncio.run(client.reject(RejectRequest(
        approval_id=uuid.UUID(_VALID_UUID),
        decided_by="u",
        reason="explanation",
    )))
    assert captured["url"] == "/approvals/reject"
    assert captured["body"]["reason"] == "explanation"
    assert "comment" not in captured["body"]


def test_probe_515_correct_path() -> None:
    """Cluster AP regression — body must carry 'modifications'."""
    from apecx_integration.control_plane.schemas.api import CorrectRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, _approval_response_json()))
    asyncio.run(client.correct(CorrectRequest(
        approval_id=uuid.UUID(_VALID_UUID),
        decided_by="u",
        modifications={"k": "v"},
    )))
    assert captured["url"] == "/approvals/correct"
    assert captured["body"]["modifications"] == {"k": "v"}
    assert "corrected_payload" not in captured["body"]


def test_probe_516_list_pending_approvals_path() -> None:
    from apecx_integration.control_plane.schemas.api import (
        ListPendingApprovalsRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {"approvals": []}))
    asyncio.run(client.list_pending_approvals(
        ListPendingApprovalsRequest(user_id="alice")
    ))
    assert captured["url"] == "/approvals/pending"


# ---------------------------------------------------------------------------
# Error mapping — probes 517-520
# ---------------------------------------------------------------------------


def test_probe_517_501_maps_to_not_implemented() -> None:
    """A 501 from the Control Plane (route still stub) must surface
    as NotImplementedError on the client side, not generic
    HTTPStatusError. The MCP tool then renders a useful message."""
    from apecx_integration.control_plane.schemas.api import (
        StartWorkflowRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(
        captured, {"detail": "T01 still pending"}, status=501
    ))
    with pytest.raises(NotImplementedError, match="T01 still pending"):
        asyncio.run(client.start_workflow(
            StartWorkflowRequest(description="x", user_id="u")
        ))


def test_probe_518_503_maps_to_dependency_error() -> None:
    """A 503 from the Control Plane (composer / executor not
    configured) must map to ControlPlaneDependencyError so the MCP
    layer can distinguish "operator misconfiguration" from "real
    HTTP failure."""
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneDependencyError,
    )
    from apecx_integration.control_plane.schemas.api import (
        StartWorkflowRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(
        captured, {"detail": "composer missing"}, status=503
    ))
    with pytest.raises(ControlPlaneDependencyError, match="composer"):
        asyncio.run(client.start_workflow(
            StartWorkflowRequest(description="x", user_id="u")
        ))


def test_probe_519_other_4xx_raises_http_status_error() -> None:
    """A 400 (bad request) or 422 (validation) should NOT be
    silently swallowed — must raise HTTPStatusError."""
    from apecx_integration.control_plane.schemas.api import (
        StartWorkflowRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(
        captured, {"detail": "validation failed"}, status=422
    ))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.start_workflow(
            StartWorkflowRequest(description="x", user_id="u")
        ))


def test_probe_520_dependency_error_default_when_no_detail() -> None:
    """If the 503 response has no 'detail' field, the dependency
    error must still carry a useful message naming the path."""
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneDependencyError,
    )
    from apecx_integration.control_plane.schemas.api import (
        StartWorkflowRequest,
    )
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {}, status=503))
    with pytest.raises(ControlPlaneDependencyError) as exc:
        asyncio.run(client.start_workflow(
            StartWorkflowRequest(description="x", user_id="u")
        ))
    assert "/workflows/start" in str(exc.value)


# ---------------------------------------------------------------------------
# Body-shape probes — 521-525
# ---------------------------------------------------------------------------


def test_probe_521_start_workflow_body_carries_schema_fields() -> None:
    from apecx_integration.control_plane.schemas.api import (
        StartWorkflowRequest,
    )
    captured: dict = {}
    response = {
        "run": _run_response_json(),
        "generated_workflow_artifact_id": _VALID_UUID,
    }
    client = _make_client(_capture_handler(captured, response))
    asyncio.run(client.start_workflow(StartWorkflowRequest(
        description="my desc", user_id="alice", preferred_executor="local",
    )))
    body = captured["body"]
    assert body["description"] == "my desc"
    assert body["user_id"] == "alice"
    assert body["preferred_executor"] == "local"


def test_probe_522_approve_body_field_exact() -> None:
    from apecx_integration.control_plane.schemas.api import ApproveRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, _approval_response_json()))
    asyncio.run(client.approve(ApproveRequest(
        approval_id=uuid.UUID(_VALID_UUID),
        decided_by="alice@example",
        comment="",
    )))
    body = captured["body"]
    assert body["decided_by"] == "alice@example"
    assert body["comment"] == ""


def test_probe_523_reject_body_no_comment_field() -> None:
    """Cluster AO regression at body level — wire format must NOT
    contain a 'comment' field that the route would extra-forbid."""
    from apecx_integration.control_plane.schemas.api import RejectRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, _approval_response_json()))
    asyncio.run(client.reject(RejectRequest(
        approval_id=uuid.UUID(_VALID_UUID),
        decided_by="u",
        reason="why not",
    )))
    body = captured["body"]
    assert "comment" not in body
    assert body["reason"] == "why not"


def test_probe_524_correct_body_no_legacy_fields() -> None:
    """Cluster AP regression at body level."""
    from apecx_integration.control_plane.schemas.api import CorrectRequest
    captured: dict = {}
    client = _make_client(_capture_handler(captured, _approval_response_json()))
    asyncio.run(client.correct(CorrectRequest(
        approval_id=uuid.UUID(_VALID_UUID),
        decided_by="u",
        modifications={"a": 1},
    )))
    body = captured["body"]
    assert "comment" not in body
    assert "corrected_payload" not in body
    assert body["modifications"] == {"a": 1}


def test_probe_525_confirm_allocation_body_finite_only() -> None:
    """confirmed_core_hours must round-trip as the same finite
    float — Pydantic's mode='before' validator catches NaN/Infinity
    on construction (cluster AL regression)."""
    from pydantic import ValidationError
    from apecx_integration.control_plane.schemas.api import (
        ConfirmAllocationRequest,
    )
    # Valid
    cr = ConfirmAllocationRequest(
        run_id=uuid.UUID(_VALID_UUID), confirmed_core_hours=10.0
    )
    assert cr.confirmed_core_hours == 10.0
    # NaN rejected
    with pytest.raises(ValidationError):
        ConfirmAllocationRequest(
            run_id=uuid.UUID(_VALID_UUID),
            confirmed_core_hours=float("nan"),
        )
    # Infinity rejected
    with pytest.raises(ValidationError):
        ConfirmAllocationRequest(
            run_id=uuid.UUID(_VALID_UUID),
            confirmed_core_hours=float("inf"),
        )


# ---------------------------------------------------------------------------
# Lifecycle + miscellany — 526-529
# ---------------------------------------------------------------------------


def test_probe_526_healthz_uses_get() -> None:
    """/healthz must be GET, not POST. Misrouting it as POST would
    surface as 405 Method Not Allowed in production."""
    captured: dict = {}
    client = _make_client(_capture_handler(captured, {"status": "ok"}))
    asyncio.run(client.healthz())
    assert captured["method"] == "GET"
    assert captured["url"] == "/healthz"


def test_probe_527_aenter_aexit_lifecycle() -> None:
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    async def _go():
        c = ControlPlaneClient("http://test.invalid")
        async with c as entered:
            assert entered is c
        # After aexit, the underlying httpx client should be closed
        assert c._client.is_closed
    asyncio.run(_go())


def test_probe_528_close_idempotent() -> None:
    """Calling close() twice must NOT raise. Allows defensive
    cleanup paths in tests + shutdown handlers without coordinating
    'who closes the client.'"""
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )
    async def _go():
        c = ControlPlaneClient("http://test.invalid")
        await c.close()
        await c.close()  # must not raise
    asyncio.run(_go())


def test_probe_529_revoke_uses_patch_with_id_in_path() -> None:
    """revoke_verified_synonym is the one client method that uses
    PATCH (not POST) and embeds an ID in the path. A regression
    here would manifest as 405 / 404."""
    from apecx_integration.control_plane.schemas.api import (
        RevokeVerifiedSynonymRequest,
    )
    captured: dict = {}
    response = {
        "verified_synonym": {
            "id": _VALID_UUID,
            "source_vocabulary": "user_query",
            "query_term": "x",
            "target_vocabulary": "violin.pathogen_id",
            "canonical_term": "y",
            "scope": None,
            "verified_by": "u",
            "verified_at": "2026-04-26T00:00:00+00:00",
            "confidence": 1.0,
            "source_run_id": None,
            "comment": None,
            "is_active": False,
            "revoked_by": "u",
            "revoked_at": "2026-04-26T00:00:00+00:00",
            "revocation_reason": "bad data",
            "superseded_by": None,
        }
    }
    client = _make_client(_capture_handler(captured, response))
    sid = uuid.UUID(_VALID_UUID)
    asyncio.run(client.revoke_verified_synonym(
        sid, RevokeVerifiedSynonymRequest(
            revocation_reason="bad data", revoked_by="u"
        )
    ))
    assert captured["method"] == "PATCH"
    assert captured["url"] == f"/verified_synonyms/{sid}"
