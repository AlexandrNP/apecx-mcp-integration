"""TX1 integration: /approvals/* against a real migrated SQLite DB.

Each test spins up a fresh FastAPI TestClient against a new SQLite
file (see tests/integration/conftest.py). No mocks: the
ProvenanceRecorder, session factory, and SQLAlchemy engine are all
real and wired by ``create_app(engine=cp_engine)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


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
                "VALUES (:id, :rid, 'synonym_approval_gate', 'LOCAL', "
                "'PAUSED_FOR_APPROVAL', '[]', '[]')"
            ),
            {"id": str(step_id), "rid": str(run_id)},
        )
    return run_id, step_id


def _create_approval(client: TestClient, run_id: UUID, step_id: UUID) -> dict:
    resp = client.post(
        "/approvals/",
        json={
            "run_id": str(run_id),
            "step_id": str(step_id),
            "kind": "hard",
            "summary": "Review proposed synonyms",
            "artifact_ids": [],
            "policy": {},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["approval"]


def test_create_approval_persists_row_and_emits_provenance(cp_client, cp_engine) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    approval = _create_approval(cp_client, run_id, step_id)

    assert approval["step_id"] == str(step_id)
    assert approval["kind"] == "hard"
    assert approval["status"] == "pending"
    assert approval["policy"]["summary"] == "Review proposed synonyms"

    # The row exists and a provenance event was chained.
    with cp_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM approval WHERE id = :id"),
            {"id": approval["id"]},
        ).scalar_one()
        assert row == "PENDING"

        prov_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM provenance_event "
                "WHERE run_id = :rid AND event_type = 'APPROVAL_REQUESTED'"
            ),
            {"rid": str(run_id)},
        ).scalar_one()
        assert prov_count == 1


def test_create_approval_rejects_mismatched_run_and_step(cp_client, cp_engine) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    wrong_run = uuid4()
    resp = cp_client.post(
        "/approvals/",
        json={
            "run_id": str(wrong_run),
            "step_id": str(step_id),
            "kind": "hard",
            "summary": "x",
        },
    )
    assert resp.status_code == 400
    assert "belongs to run" in resp.json()["detail"]


def test_create_approval_rejects_unknown_step(cp_client) -> None:
    resp = cp_client.post(
        "/approvals/",
        json={
            "run_id": str(uuid4()),
            "step_id": str(uuid4()),
            "kind": "hard",
            "summary": "x",
        },
    )
    assert resp.status_code == 404


def test_approve_transitions_to_approved(cp_client, cp_engine) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    approval = _create_approval(cp_client, run_id, step_id)
    resp = cp_client.post(
        "/approvals/approve",
        json={"approval_id": approval["id"], "comment": "lgtm", "decided_by": "alex"},
    )
    assert resp.status_code == 200
    decided = resp.json()["approval"]
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "alex"
    assert decided["comment"] == "lgtm"
    assert decided["decided_at"] is not None


def test_reject_transitions_to_rejected(cp_client, cp_engine) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    approval = _create_approval(cp_client, run_id, step_id)
    resp = cp_client.post(
        "/approvals/reject",
        json={
            "approval_id": approval["id"],
            "reason": "proposed synonyms include wrong pathogen",
            "decided_by": "alex",
        },
    )
    assert resp.status_code == 200
    decided = resp.json()["approval"]
    assert decided["status"] == "rejected"
    assert decided["comment"] == "proposed synonyms include wrong pathogen"


def test_correct_transitions_with_modifications(cp_client, cp_engine) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    approval = _create_approval(cp_client, run_id, step_id)
    mods = {"synonyms": {"vaccinia": ["VACV"]}}
    resp = cp_client.post(
        "/approvals/correct",
        json={
            "approval_id": approval["id"],
            "modifications": mods,
            "decided_by": "alex",
        },
    )
    assert resp.status_code == 200
    decided = resp.json()["approval"]
    assert decided["status"] == "approved_with_modifications"
    assert decided["policy"]["modifications"] == mods


def test_double_approve_raises_409(cp_client, cp_engine) -> None:
    run_id, step_id = _seed_run_and_step(cp_engine)
    approval = _create_approval(cp_client, run_id, step_id)
    r1 = cp_client.post(
        "/approvals/approve",
        json={"approval_id": approval["id"]},
    )
    assert r1.status_code == 200
    r2 = cp_client.post(
        "/approvals/approve",
        json={"approval_id": approval["id"]},
    )
    assert r2.status_code == 409
    assert "already decided" in r2.json()["detail"]


def test_decide_unknown_approval_is_404(cp_client) -> None:
    resp = cp_client.post(
        "/approvals/approve",
        json={"approval_id": str(uuid4())},
    )
    assert resp.status_code == 404


def test_get_approval_returns_current_state_for_polling(cp_client, cp_engine) -> None:
    """GET /approvals/{id} is what the nanobrain ApprovalStep polls while
    paused. It must reflect status transitions as decisions land.
    """
    run_id, step_id = _seed_run_and_step(cp_engine)
    created = _create_approval(cp_client, run_id, step_id)
    aid = created["id"]

    r_pending = cp_client.get(f"/approvals/{aid}")
    assert r_pending.status_code == 200
    assert r_pending.json()["approval"]["status"] == "pending"

    cp_client.post("/approvals/approve", json={"approval_id": aid, "decided_by": "alex"})
    r_decided = cp_client.get(f"/approvals/{aid}")
    assert r_decided.status_code == 200
    body = r_decided.json()["approval"]
    assert body["status"] == "approved"
    assert body["decided_by"] == "alex"


def test_get_unknown_approval_is_404(cp_client) -> None:
    resp = cp_client.get(f"/approvals/{uuid4()}")
    assert resp.status_code == 404


def test_list_pending_filters_by_user_and_status(cp_client, cp_engine) -> None:
    run_a, step_a = _seed_run_and_step(cp_engine, user_id="alex")
    run_b, step_b = _seed_run_and_step(cp_engine, user_id="bob")
    # alex has two pending + one approved; bob has one pending.
    a1 = _create_approval(cp_client, run_a, step_a)
    _create_approval(cp_client, run_a, step_a)  # a2 still pending
    _create_approval(cp_client, run_b, step_b)  # b's pending
    # Approve a1 to test that it's filtered out.
    cp_client.post("/approvals/approve", json={"approval_id": a1["id"]})

    resp = cp_client.post("/approvals/pending", json={"user_id": "alex"})
    assert resp.status_code == 200
    approvals = resp.json()["approvals"]
    assert len(approvals) == 1, approvals
    assert approvals[0]["status"] == "pending"


def test_decision_emits_provenance(cp_client, cp_engine) -> None:
    """An APPROVAL_REQUESTED and an APPROVAL_DECIDED event both land in
    the per-run hash chain; chain validates end-to-end.
    """
    from apecx_integration.control_plane.db import make_session_factory
    from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder

    run_id, step_id = _seed_run_and_step(cp_engine)
    approval = _create_approval(cp_client, run_id, step_id)
    cp_client.post(
        "/approvals/approve",
        json={"approval_id": approval["id"], "decided_by": "alex"},
    )

    # Build a recorder pointing at the same engine to validate.
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    recorder.validate(run_id)

    with cp_engine.connect() as conn:
        types = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT event_type FROM provenance_event "
                    "WHERE run_id = :rid ORDER BY timestamp"
                ),
                {"rid": str(run_id)},
            ).all()
        ]
    assert types == ["APPROVAL_REQUESTED", "APPROVAL_DECIDED"]
