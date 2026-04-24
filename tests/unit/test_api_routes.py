"""Wiring tests for the Control Plane API.

Route status as of 2026-04-22:
  * Persistence-only (approvals, runs/*, verified-synonyms): wired,
    covered by integration tests in tests/integration/test_api_*.py.
  * Composer-backed (/workflows/start | plan | diff | /hpc/estimate |
    /hpc/confirm): wired, integration-tested under the venv.
  * HPC export lane (/hpc/submit, /hpc/export): **still 501** — they
    genuinely need T04 or T05 (demoted optional per Round 3).

Body-shape + request-validation tests live in integration (where a
real composer fixture is wired into ``create_app``). A bare
``create_app()`` trips the ``get_composer`` 503 gate before pydantic
body-validation runs, so the classic "422 for bad body" pattern
isn't exercisable here.
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
def test_hpc_export_lane_still_501(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    """The two HPC-export routes remain 501 pending T04/T05 work.

    Both routes are registered (OpenAPI schema test confirms it) but
    the handlers raise 501 with a task-ref detail string so operators
    can trace the block.
    """
    resp = client.post(path, json=payload)
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert "not implemented" in detail.lower()
    assert (
        "implementation_plan.md" in detail or "T" in detail
    ), f"501 detail for {path} does not reference a task: {detail!r}"


def test_composer_backed_routes_return_503_without_composer(
    client: TestClient,
) -> None:
    """``create_app()`` with no composer → /workflows/start returns
    503 from the get_composer dependency, NOT 501. Canary: if anyone
    makes the route always-live or drops the DI, this catches it.
    """
    resp = client.post(
        "/workflows/start",
        json={"description": "ok", "user_id": "alex"},
    )
    assert resp.status_code == 503
    assert "Composer is not configured" in resp.json()["detail"]
