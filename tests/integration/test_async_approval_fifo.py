"""Cluster AE — /approvals/pending and /runs/status return random order.

Approval has no ``created_at`` column. The route's ``ORDER BY
ApprovalORM.id`` orders by random UUID — lex-largest first, then
descending. So an operator polling for "the oldest pending
approval" gets a random one instead.

That's not a race-condition bug per se, but it's exactly the
kind of reliability/UX issue the friction-log #17 entry warns
about: "ORDER BY id DESC on a random-UUID PK is a bug, not an
ordering." Same shape, lower stakes than cluster AC, but real
for any operator draining a backlog.

The Step list at /runs/status has the same flavor: it orders by
``StepORM.started_at.asc().nulls_last(), StepORM.id``. For
multiple PENDING steps (started_at = NULL), the tiebreaker is
random UUID. Less critical because Step rows aren't authored
in production yet, but the same fix shape applies.

Fix: migration 0005 adds ``approval.created_at``. ``create_approval``
sets it on insert. ``list_pending_approvals`` orders by
``created_at ASC, id ASC`` (oldest first; FIFO drain).

Test: insert 4 approvals with deliberately inverted UUIDs and
sequenced created_at timestamps. Assert /approvals/pending
returns them in CREATION order, not UUID-lex order.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def _seed_run_step_approval(
    cp_engine: Engine,
    *,
    user_id: str,
    approval_id: UUID,
) -> tuple[UUID, UUID]:
    """Insert a Run + Step + PENDING Approval with a controlled
    approval UUID. Returns (run_id, step_id).

    Each call sleeps briefly so consecutive seedings have distinct
    microsecond-resolution created_at on the approval (under any
    column we add).
    """
    run_id = uuid4()
    step_id = uuid4()
    now_iso = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, 'PAUSED', :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "ts": now_iso},
        )
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, "
                "status, input_artifact_ids, output_artifact_ids, "
                "created_at) "
                "VALUES (:id, :rid, 'gate', 'LOCAL', 'PENDING', "
                "'[]', '[]', :ts)"
            ),
            {"id": str(step_id), "rid": str(run_id), "ts": now_iso},
        )
        conn.execute(
            text(
                "INSERT INTO approval (id, step_id, kind, status, "
                "policy, created_at) VALUES "
                "(:id, :sid, 'HARD', 'PENDING', '{}', :ts)"
            ),
            {
                "id": str(approval_id),
                "sid": str(step_id),
                "ts": now_iso,
            },
        )
    # Force microsecond progression so the next seeding is
    # observably newer.
    time.sleep(0.001)
    return run_id, step_id


def test_list_pending_returns_oldest_first_not_uuid_lex_order(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Insert 4 approvals with UUIDs deliberately inverted relative
    to creation order. Assert /approvals/pending returns them
    oldest-first.

    Currently fails: route orders by UUID, so the apparent order
    is the lex order of the chosen UUIDs (alice's first-inserted
    has the LARGEST UUID, last-inserted has the SMALLEST), giving
    a reverse-creation-order response.
    """
    # UUIDs chosen so seeding-order 0..3 maps to lex order 3..0.
    # Picked manually to invert lex vs creation completely.
    inverted_uuids = [
        UUID("ffffffff-ffff-4fff-bfff-fffffffff003"),  # 1st inserted, lex-largest
        UUID("aaaaaaaa-aaaa-4aaa-baaa-aaaaaaaaa002"),
        UUID("55555555-5555-4555-b555-555555555001"),
        UUID("00000000-0000-4000-8000-000000000000"),  # 4th inserted, lex-smallest
    ]
    seeded: list[UUID] = []
    for aid in inverted_uuids:
        _seed_run_step_approval(cp_engine, user_id="alex", approval_id=aid)
        seeded.append(aid)

    resp = cp_client.post("/approvals/pending", json={"user_id": "alex"})
    assert resp.status_code == 200, resp.text
    returned_ids = [UUID(a["id"]) for a in resp.json()["approvals"]]

    assert returned_ids == seeded, (
        f"BUG: /approvals/pending returned approvals in "
        f"{returned_ids} (UUID-lex descending) but operators expect "
        f"oldest-first creation order {seeded}. The route uses "
        "ORDER BY ApprovalORM.id where id is a random uuid4 — that "
        "picks lex order, not creation order. Fix: add a created_at "
        "column on Approval and ORDER BY it."
    )


def test_list_pending_filter_by_user_remains_intact(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Sanity: the user filter still works under the new ordering.
    Adds approvals for two users, asserts each user sees only
    their own.
    """
    _seed_run_step_approval(
        cp_engine,
        user_id="alex",
        approval_id=UUID("11111111-1111-4111-9111-111111111111"),
    )
    _seed_run_step_approval(
        cp_engine,
        user_id="bob",
        approval_id=UUID("22222222-2222-4222-9222-222222222222"),
    )

    alex_resp = cp_client.post("/approvals/pending", json={"user_id": "alex"})
    bob_resp = cp_client.post("/approvals/pending", json={"user_id": "bob"})
    assert alex_resp.status_code == 200
    assert bob_resp.status_code == 200

    alex_ids = {a["id"] for a in alex_resp.json()["approvals"]}
    bob_ids = {a["id"] for a in bob_resp.json()["approvals"]}

    assert "11111111-1111-4111-9111-111111111111" in alex_ids
    assert "22222222-2222-4222-9222-222222222222" in bob_ids
    assert "22222222-2222-4222-9222-222222222222" not in alex_ids
    assert "11111111-1111-4111-9111-111111111111" not in bob_ids
