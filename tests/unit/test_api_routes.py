"""Wiring tests for the Control Plane API (TX1).

Routes fall into two groups:
  * Persistence-only (approvals, runs/list, runs/status, runs/artifact):
    wired to real T09 persistence — covered by integration tests in
    tests/integration/test_api_*.py, not here.
  * Downstream-dependent (workflows/*, hpc/*): still stubs that raise
    HTTP 501 because the composer / HPC-export / differ tasks have not
    landed. This file verifies their shape and that the 501 detail
    points at the task that unblocks them.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from apecx_integration.control_plane.app import create_app
from fastapi.testclient import TestClient


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
