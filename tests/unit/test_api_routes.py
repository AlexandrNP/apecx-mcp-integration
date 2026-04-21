"""Wiring tests for the Control Plane API (TX1).

Round 3: every route is a stub that raises HTTP 501. These tests verify that
(a) the routes are registered under the documented paths, (b) the request
envelopes validate and reach the handler, and (c) the 501 detail message
includes a task reference so operators can trace what is missing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apecx_integration.control_plane.app import create_app


@pytest.fixture(name="client")
def _client() -> TestClient:
    return TestClient(create_app())


def test_openapi_schema_is_served(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["info"]["title"] == "APECx Control Plane"
    paths = set(body["paths"].keys())
    assert {
        "/healthz",
        "/workflows/start",
        "/workflows/plan",
        "/workflows/diff",
        "/approvals/",
        "/approvals/approve",
        "/approvals/reject",
        "/approvals/correct",
        "/approvals/pending",
        "/runs/list",
        "/runs/status",
        "/runs/artifact",
        "/hpc/estimate",
        "/hpc/confirm",
        "/hpc/submit",
        "/hpc/export",
    }.issubset(paths)


def test_healthz_is_the_only_non_stub(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "phase": "scaffold"}


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/workflows/start",
            {"description": "Run VIOLIN × BV-BRC", "user_id": "alex"},
        ),
        (
            "/workflows/plan",
            {"description": "Run VIOLIN × BV-BRC"},
        ),
        (
            "/workflows/diff",
            {"run_id": str(uuid4())},
        ),
        (
            "/approvals/",
            {
                "run_id": str(uuid4()),
                "step_id": str(uuid4()),
                "kind": "soft",
                "summary": "Review proposed synonyms",
            },
        ),
        (
            "/approvals/approve",
            {"approval_id": str(uuid4())},
        ),
        (
            "/approvals/reject",
            {"approval_id": str(uuid4()), "reason": "wrong pathogen"},
        ),
        (
            "/approvals/correct",
            {
                "approval_id": str(uuid4()),
                "modifications": {"synonyms": ["A", "B"]},
            },
        ),
        (
            "/approvals/pending",
            {"user_id": "alex"},
        ),
        (
            "/runs/list",
            {"user_id": "alex"},
        ),
        (
            "/runs/status",
            {"run_id": str(uuid4())},
        ),
        (
            "/runs/artifact",
            {"artifact_id": str(uuid4())},
        ),
        (
            "/hpc/estimate",
            {"run_id": str(uuid4())},
        ),
        (
            "/hpc/confirm",
            {"run_id": str(uuid4()), "confirmed_core_hours": 10.0},
        ),
        (
            "/hpc/submit",
            {"run_id": str(uuid4()), "executor": "pbs_bundle"},
        ),
        (
            "/hpc/export",
            {
                "run_id": str(uuid4()),
                "target_system": "polaris",
                "output_directory": "/tmp/bundle",
            },
        ),
    ],
)
def test_every_route_stubs_with_501_and_task_ref(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    resp = client.post(path, json=payload)
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert "not implemented" in detail.lower()
    # Every stub must point at a task or doc anchor so ops can trace it.
    assert (
        "implementation_plan.md" in detail or "T" in detail
    ), f"501 detail for {path} does not reference a task: {detail!r}"


def test_request_validation_rejects_empty_description(client: TestClient) -> None:
    resp = client.post(
        "/workflows/start",
        json={"description": "", "user_id": "alex"},
    )
    assert resp.status_code == 422


def test_request_validation_rejects_unknown_executor(client: TestClient) -> None:
    resp = client.post(
        "/workflows/start",
        json={
            "description": "ok",
            "user_id": "alex",
            "preferred_executor": "not_a_real_executor",
        },
    )
    assert resp.status_code == 422


def test_request_validation_rejects_extra_fields(client: TestClient) -> None:
    resp = client.post(
        "/workflows/start",
        json={"description": "ok", "user_id": "alex", "surprise_field": 1},
    )
    assert resp.status_code == 422
