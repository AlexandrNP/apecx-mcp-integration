"""Cluster AK — /hpc/confirm marks stale row when /hpc/estimate races.

Cluster AC fixed the chronology-ordering bug: ``/hpc/confirm``
now orders by ``(created_at DESC, id DESC)`` to find the latest
estimate. But the SELECT-then-UPDATE pattern still has a race
window: if ``/hpc/estimate`` inserts a NEW row between the
confirm's SELECT and UPDATE, confirm marks the row that WAS
latest at SELECT time — by the time the UPDATE commits, that
row is no longer the latest, and the new latest is unconfirmed.

Result: the API returns 200 ("confirmed=True") even though the
LATEST estimate (the one any subsequent read will find) carries
``user_confirmed=False``. Silent failure of the user's intent.

Fail-fast fix: conditional UPDATE that fails if a newer row
exists for the same run_id. Rowcount==0 → 409 with a clear
"newer estimate appeared during your confirm; re-fetch and
re-confirm." Caller decides whether to auto-retry or surface to
the user.

This test simulates the timing by:
  1. Inserting estimate R1 for run.
  2. Calling /hpc/confirm — but race the SELECT with a manual
     INSERT of estimate R2 (newer) before the UPDATE lands.

Done deterministically via a SQLAlchemy ``do_orm_execute`` hook
that pauses after the SELECT for ~50ms. During the pause, the
test thread inserts R2.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text


pytestmark = pytest.mark.integration


def _seed_run_with_workflow(cp_engine: Engine) -> UUID:
    run_id = uuid4()
    artifact_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', '/tmp/none', "
                "'sha256-placeholder', 1, 'application/x-yaml', :ts)"
            ),
            {"id": str(artifact_id), "rid": str(run_id), "ts": now},
        )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return run_id


def _insert_estimate(
    cp_engine: Engine,
    *,
    run_id: UUID,
    estimate_id: UUID,
    estimated_core_hours: float,
    created_at: datetime | None = None,
) -> None:
    """Insert via SQLAlchemy ORM so the stored ``created_at``
    format matches what /hpc/estimate produces in production.

    Earlier helpers used ``datetime.isoformat()`` strings which
    produced 'YYYY-MM-DDTHH:MM:SS+TZ' — but the ORM produces
    'YYYY-MM-DD HH:MM:SS' (space-separator, naive). Mixing the
    two in one column breaks lex comparison and makes the
    cluster AK race-detection predicate misfire.
    """
    from apecx_integration.control_plane.db import make_session_factory
    from apecx_integration.control_plane.models.entities import (
        AllocationEstimate as AEORM,
    )

    factory = make_session_factory(cp_engine)
    with factory() as session:
        session.add(
            AEORM(
                id=estimate_id,
                run_id=run_id,
                estimated_core_hours=estimated_core_hours,
                estimated_wall_time_seconds=estimated_core_hours * 3600.0,
                endpoint="polaris",
                user_confirmed=False,
                created_at=created_at or datetime.now(UTC),
            )
        )
        session.commit()


def test_confirm_rejects_when_newer_estimate_appears_mid_flight(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Insert R1, then call /hpc/confirm with a hook that inserts
    R2 BETWEEN the route's SELECT and the in-session UPDATE-during-
    commit. Asserts that the post-state has the actual LATEST
    confirmed (route picked R2) OR the route returned 409
    ("newer estimate appeared"). Silent failure (R1 confirmed,
    R2 unconfirmed, response=200) is the bug.

    The race window inside the route:

        latest = session.execute(SELECT … ORDER BY created_at DESC).scalar_one()
        # ← race here: another writer INSERTs R2.
        latest.user_confirmed = True
        latest.user_confirmed_at = datetime.now(UTC)
        session.commit()    # ← UPDATE on `latest` (= R1, stale)

    The ``before_flush`` SQLAlchemy event fires during
    ``session.commit()`` BEFORE the UPDATE statement is sent —
    that's the right pause point for the racer's INSERT.
    """
    run_id = _seed_run_with_workflow(cp_engine)

    r1_id = UUID("11111111-1111-4111-8111-111111111111")
    _insert_estimate(
        cp_engine, run_id=run_id, estimate_id=r1_id, estimated_core_hours=10.0
    )

    update_seen = threading.Event()
    racer_done = threading.Event()
    racer_error: list[BaseException] = []

    def _on_orm_execute(state):
        try:
            stmt_str = str(state.statement)
        except Exception:
            return
        # Match the route's UPDATE-with-NOT-EXISTS statement —
        # signal the racer THEN pause BEFORE the UPDATE actually
        # runs. This widens the window between the route's SELECT
        # (already done) and the UPDATE (about to fire) into a
        # deterministic ~50ms hold.
        if (
            "UPDATE allocation_estimate" in stmt_str
            and not update_seen.is_set()
        ):
            update_seen.set()
            racer_done.wait(timeout=5)

    def _racer() -> None:
        try:
            if not update_seen.wait(timeout=5):
                racer_error.append(
                    RuntimeError(
                        "UPDATE event was never observed — the route "
                        "may have short-circuited (e.g., 422 below the "
                        "ceiling check) before reaching the UPDATE"
                    )
                )
                return
            r2_id = UUID("22222222-2222-4222-9222-222222222222")
            _insert_estimate(
                cp_engine,
                run_id=run_id,
                estimate_id=r2_id,
                estimated_core_hours=25.0,
                created_at=datetime.now(UTC),
            )
        except BaseException as exc:  # noqa: BLE001
            racer_error.append(exc)
        finally:
            racer_done.set()

    racer = threading.Thread(target=_racer)
    racer.start()

    from sqlalchemy.orm import Session as ORMSession

    event.listen(ORMSession, "do_orm_execute", _on_orm_execute)
    try:
        resp = cp_client.post(
            "/hpc/confirm",
            json={
                "run_id": str(run_id),
                "confirmed_core_hours": 30.0,
            },
        )
    finally:
        event.remove(ORMSession, "do_orm_execute", _on_orm_execute)

    racer.join(timeout=10)
    assert not racer_error, f"racer thread crashed: {racer_error[0]!r}"
    assert update_seen.is_set(), (
        "the UPDATE event was never observed — test isn't "
        "exercising the race window"
    )

    # Read both rows from a fresh sqlite3 connection.
    import sqlite3
    db_path = str(cp_engine.url).replace("sqlite:///", "")
    cp_engine.dispose()
    raw = sqlite3.connect(db_path)
    rows = raw.execute(
        "SELECT id, user_confirmed FROM allocation_estimate "
        "WHERE run_id = ? ORDER BY created_at",
        (str(run_id),),
    ).fetchall()
    raw.close()

    print(f"\n[confirm-race] resp={resp.status_code} rows={rows}")

    assert len(rows) == 2, f"expected 2 estimates, got {len(rows)}: {rows}"
    older_id, older_confirmed = rows[0]
    newer_id, newer_confirmed = rows[1]

    if resp.status_code == 200:
        assert newer_confirmed in (1, True), (
            f"BUG (cluster AK): /hpc/confirm returned 200 (claiming "
            f"success) but the LATEST estimate {newer_id} stayed "
            f"unconfirmed; the OLDER estimate {older_id} was marked "
            "confirmed instead (user_confirmed=1 in DB). Silent "
            "failure: subsequent reads of 'latest' show an "
            "unconfirmed row, but the API said the user's confirm "
            "succeeded. Fix: conditional UPDATE WHERE id = :latest_id "
            "AND NOT EXISTS (newer row for same run_id); rowcount==0 "
            "→ 409 with reason 'newer estimate appeared, re-fetch.'"
        )
    elif resp.status_code == 409:
        # Route detected the race; loser sees a clear conflict.
        # Either row may be unconfirmed — caller will re-fetch and
        # confirm the actual latest in a follow-up call.
        pass
    else:
        pytest.fail(
            f"unexpected response {resp.status_code}: {resp.text}"
        )
