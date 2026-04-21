"""TX1 integration: /runs/list, /runs/status, /runs/artifact.

Each test exercises the real FastAPI TestClient against a fresh migrated
SQLite DB from the ``cp_client`` / ``cp_engine`` fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


def _insert_run(
    engine: Engine,
    *,
    user_id: str = "alex",
    status_value: str = "PENDING",
    created_at: datetime | None = None,
) -> UUID:
    run_id = uuid4()
    ts = (created_at or datetime.now(timezone.utc)).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, :st, :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "st": status_value, "ts": ts},
        )
    return run_id


def _insert_step(
    engine: Engine,
    run_id: UUID,
    *,
    step_name: str = "example",
    status_value: str = "PENDING",
) -> UUID:
    step_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, status, "
                "input_artifact_ids, output_artifact_ids) "
                "VALUES (:id, :rid, :name, 'LOCAL', :st, '[]', '[]')"
            ),
            {
                "id": str(step_id),
                "rid": str(run_id),
                "name": step_name,
                "st": status_value,
            },
        )
    return step_id


def _insert_artifact(engine: Engine, run_id: UUID) -> UUID:
    artifact_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, content_hash, "
                "size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'INPUT', '/tmp/art', :hash, 12, "
                "'application/octet-stream', :ts)"
            ),
            {
                "id": str(artifact_id),
                "rid": str(run_id),
                "hash": "a" * 64,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    return artifact_id


def test_list_runs_returns_user_scoped_rows(cp_client: TestClient, cp_engine) -> None:
    t0 = datetime.now(timezone.utc)
    r_alex_new = _insert_run(cp_engine, user_id="alex", created_at=t0)
    r_alex_old = _insert_run(cp_engine, user_id="alex", created_at=t0 - timedelta(hours=1))
    _insert_run(cp_engine, user_id="bob")

    resp = cp_client.post("/runs/list", json={"user_id": "alex"})
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert {r["id"] for r in runs} == {str(r_alex_new), str(r_alex_old)}
    # Ordered newest-first by created_at.
    assert runs[0]["id"] == str(r_alex_new)


def test_list_runs_respects_status_filter(cp_client: TestClient, cp_engine) -> None:
    r_running = _insert_run(cp_engine, user_id="alex", status_value="RUNNING")
    _insert_run(cp_engine, user_id="alex", status_value="COMPLETED")

    resp = cp_client.post(
        "/runs/list",
        json={"user_id": "alex", "status_filter": "running"},
    )
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == str(r_running)


def test_list_runs_respects_limit(cp_client: TestClient, cp_engine) -> None:
    for _ in range(5):
        _insert_run(cp_engine, user_id="alex")
    resp = cp_client.post("/runs/list", json={"user_id": "alex", "limit": 2})
    assert resp.status_code == 200
    assert len(resp.json()["runs"]) == 2


def test_get_status_returns_run_steps_and_pending_approval(
    cp_client: TestClient, cp_engine
) -> None:
    run_id = _insert_run(cp_engine)
    step_id = _insert_step(cp_engine, run_id, step_name="synonym_gate",
                           status_value="PAUSED_FOR_APPROVAL")
    _insert_step(cp_engine, run_id, step_name="entity_extraction",
                 status_value="COMPLETED")
    # Create a pending approval via the real endpoint (also exercises that path).
    cr = cp_client.post(
        "/approvals/",
        json={
            "run_id": str(run_id),
            "step_id": str(step_id),
            "kind": "hard",
            "summary": "review synonyms",
        },
    )
    assert cr.status_code == 200
    approval_id = cr.json()["approval"]["id"]

    resp = cp_client.post("/runs/status", json={"run_id": str(run_id)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["id"] == str(run_id)
    assert len(body["steps"]) == 2
    assert body["pending_approval"] is not None
    assert body["pending_approval"]["id"] == approval_id


def test_get_status_no_pending_approval(cp_client: TestClient, cp_engine) -> None:
    run_id = _insert_run(cp_engine)
    _insert_step(cp_engine, run_id)
    resp = cp_client.post("/runs/status", json={"run_id": str(run_id)})
    assert resp.status_code == 200
    assert resp.json()["pending_approval"] is None


def test_get_status_404_on_unknown_run(cp_client: TestClient) -> None:
    resp = cp_client.post("/runs/status", json={"run_id": str(uuid4())})
    assert resp.status_code == 404


def test_get_artifact_returns_metadata_with_inline_omitted(
    cp_client: TestClient, cp_engine
) -> None:
    run_id = _insert_run(cp_engine)
    artifact_id = _insert_artifact(cp_engine, run_id)

    resp = cp_client.post("/runs/artifact", json={"artifact_id": str(artifact_id)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact"]["id"] == str(artifact_id)
    assert body["inline_bytes"] is None
    assert "T11" in (body["reason_inline_omitted"] or "")


def test_get_artifact_404_on_unknown_artifact(cp_client: TestClient) -> None:
    resp = cp_client.post("/runs/artifact", json={"artifact_id": str(uuid4())})
    assert resp.status_code == 404
