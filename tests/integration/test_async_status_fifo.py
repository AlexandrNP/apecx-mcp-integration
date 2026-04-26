"""Cluster AH — /runs/status returns random UUID order for PENDING items.

Two more spots that share cluster AE's anti-pattern:

1. ``/runs/status`` picks ONE pending approval to surface in
   the response (the operator's "what's blocking this run?"
   indicator). The pick uses ``ORDER BY ApprovalORM.id`` —
   random uuid4 → random pick. An operator looking at a run
   with 3 pending approvals sees a different one each time
   they refresh, which is confusing and breaks the "fix the
   oldest blocker first" mental model.

2. ``/runs/status`` returns the run's steps ordered by
   ``(started_at ASC NULLS LAST, id)``. PENDING steps have
   ``started_at = NULL`` so they fall back to id-ASC — random
   uuid4 → random list order.

Step rows aren't authored by production code today, but the
route IS exercised, and any framework change that starts writing
Step rows would expose this immediately. Migration 0006 adds
``step.created_at`` preemptively (zero backfill cost — table is
empty in production).

Fix: route now orders both queries by their respective
``created_at`` columns with id as tiebreak. Same shape as
clusters AC, AE.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def _seed_run(cp_engine: Engine, *, user_id: str = "alex") -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, 'PAUSED', :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "ts": now},
        )
    return run_id


def _seed_step(
    cp_engine: Engine,
    run_id: UUID,
    *,
    step_id: UUID,
    step_name: str,
) -> None:
    """Insert a PENDING step with a controlled UUID. Sleep 1ms so
    consecutive calls produce strictly-increasing created_at."""
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, "
                "status, input_artifact_ids, output_artifact_ids, "
                "created_at) "
                "VALUES (:id, :rid, :name, 'LOCAL', 'PENDING', "
                "'[]', '[]', :ts)"
            ),
            {
                "id": str(step_id),
                "rid": str(run_id),
                "name": step_name,
                "ts": now,
            },
        )
    time.sleep(0.001)


def _seed_approval(
    cp_engine: Engine,
    *,
    step_id: UUID,
    approval_id: UUID,
) -> None:
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO approval (id, step_id, kind, status, "
                "policy, created_at) VALUES "
                "(:id, :sid, 'HARD', 'PENDING', '{}', :ts)"
            ),
            {"id": str(approval_id), "sid": str(step_id), "ts": now},
        )
    time.sleep(0.001)


def test_get_status_pending_approval_picker_is_oldest_first(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Three pending approvals on the same run, with deliberately-
    inverted UUIDs vs creation order. /runs/status must surface
    the OLDEST pending approval as ``pending_approval``, not the
    lex-smallest UUID one.

    Without the fix: pick_approval orders by ApprovalORM.id, so
    the response surfaces the smallest-UUID approval — which is
    the LAST inserted in this test (UUIDs inverted).
    """
    run_id = _seed_run(cp_engine)

    # One step holds the approvals (FK requirement). Step UUID
    # doesn't matter for this test.
    step_id = uuid4()
    _seed_step(cp_engine, run_id, step_id=step_id, step_name="approve")

    # Three approvals: oldest has lex-largest UUID; newest has
    # lex-smallest UUID.
    oldest_aid = UUID("ffffffff-ffff-4fff-bfff-ffffffffaa11")
    middle_aid = UUID("88888888-8888-4888-b888-888888888888")
    newest_aid = UUID("00000000-0000-4000-8000-000000000033")
    _seed_approval(cp_engine, step_id=step_id, approval_id=oldest_aid)
    _seed_approval(cp_engine, step_id=step_id, approval_id=middle_aid)
    _seed_approval(cp_engine, step_id=step_id, approval_id=newest_aid)

    resp = cp_client.post("/runs/status", json={"run_id": str(run_id)})
    assert resp.status_code == 200, resp.text

    surfaced = resp.json()["pending_approval"]
    assert surfaced is not None, "expected a pending approval to surface"
    assert UUID(surfaced["id"]) == oldest_aid, (
        f"BUG: /runs/status surfaced pending_approval={surfaced['id']} "
        f"but the OLDEST pending approval is {oldest_aid}. The picker "
        "uses ORDER BY ApprovalORM.id (random uuid4) — it picked the "
        "lex-smallest UUID rather than the chronologically-oldest "
        "approval. Fix: order by (created_at, id)."
    )


def test_get_status_step_list_pending_steps_in_creation_order(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Three PENDING steps on the same run, with deliberately-
    inverted UUIDs vs creation order. /runs/status must list them
    in creation order, not UUID-lex order, so an operator looking
    at the run's step list sees a stable, meaningful sequence.

    Without the fix: PENDING steps fall back to ORDER BY
    StepORM.id and appear in random order.
    """
    run_id = _seed_run(cp_engine)

    inverted_step_uuids = [
        UUID("ffffffff-ffff-4fff-bfff-ffffffffbb01"),
        UUID("88888888-8888-4888-b888-8888888bb002"),
        UUID("00000000-0000-4000-8000-00000000bb03"),
    ]
    seeded: list[UUID] = []
    for sid in inverted_step_uuids:
        _seed_step(cp_engine, run_id, step_id=sid, step_name=f"s_{sid.int}")
        seeded.append(sid)

    resp = cp_client.post("/runs/status", json={"run_id": str(run_id)})
    assert resp.status_code == 200, resp.text

    returned = [UUID(s["id"]) for s in resp.json()["steps"]]
    assert returned == seeded, (
        f"BUG: /runs/status returned PENDING steps in {returned} "
        f"(UUID-lex order) but operators expect creation order "
        f"{seeded}. Fix: order by (started_at NULLS LAST, "
        "created_at, id)."
    )
