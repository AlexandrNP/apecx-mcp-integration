"""Integration: the SERVE lifespan actually runs the RunStateSweeper end-to-end.

This closes the wiring gap the scan flagged: the sweeper was built + unit-tested
(see ``test_run_state_sweeper.py``) but was never invoked at serve time. Here we
drive the REAL app lifespan (``create_app(..., start_monitor=True)``) against a
REAL Alembic-migrated SQLite DB and prove that a run stuck in RUNNING past the
stale threshold gets reaped to FAILED by the background sweep loop — no mocks.

Lifespan-driving mechanism: ``fastapi.testclient.TestClient(app)`` as a context
manager. Entering the ``with`` block runs the app's lifespan startup (which
spawns the sweep loop task alongside the InfraMonitor task); the background loop
runs on the TestClient's event-loop thread while the test thread sleeps. This is
the same lifespan pattern the rest of the integration suite uses (grep:
``with TestClient`` in tests/integration/test_probe_batch_30_sweeper_revoke.py).

The InfraMonitor task (docker polling) also starts with start_monitor=True, but
it runs in its own task with try/except around every tick, and a fresh
orchestrator singleton has no READY backends so its first status() call returns
without any docker work — it neither hangs nor interferes with the sweeper task.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.models.entities import Run as RunORM
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import RunStatus

pytestmark = pytest.mark.integration


def _insert_stale_run(engine, *, created_at: datetime, user_id: str = "alex") -> UUID:
    """Insert a run in RUNNING with NO provenance events and an old created_at —
    the exact staleness construction used by test_run_state_sweeper._insert_run
    (raw insert of id/user_id/status/created_at; created_at is the fallback age
    when a run has no provenance events)."""
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO run (id, user_id, status, created_at) VALUES (:id, :uid, :st, :ts)"),
            {
                "id": str(run_id),
                "uid": user_id,
                "st": "RUNNING",
                "ts": created_at.isoformat(),
            },
        )
    return run_id


def test_serve_lifespan_sweeps_stale_run_to_failed(cp_engine, monkeypatch) -> None:
    # Must be set BEFORE create_app so _resolve_sweep_interval picks up the fast
    # interval (the loop sleeps interval FIRST, then sweeps).
    monkeypatch.setenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", "0.2")

    now = datetime.now(UTC)
    run_id = _insert_stale_run(cp_engine, created_at=now - timedelta(hours=1))

    app = create_app(
        engine=cp_engine,
        recorder=ProvenanceRecorder(make_session_factory(cp_engine)),
        start_monitor=True,
    )

    # Entering the TestClient context runs the lifespan startup, which spawns the
    # background sweep loop. Sleep across several 0.2s intervals so it fires.
    with TestClient(app):
        time.sleep(2.0)

    # Re-query via a fresh session factory on the same engine.
    session_factory = make_session_factory(cp_engine)
    with session_factory() as session:
        run = session.execute(select(RunORM).where(RunORM.id == run_id)).scalars().one()
        assert run.status is RunStatus.FAILED, f"stale run was not swept: status={run.status}"

        events = session.execute(
            text(
                "SELECT event_type, actor FROM provenance_event "
                "WHERE run_id = :rid ORDER BY timestamp"
            ),
            {"rid": str(run_id)},
        ).all()
    assert any(e[0] == "RUN_FAILED" and e[1] == "run_state_sweeper" for e in events), (
        f"sweeper did not record a RUN_FAILED provenance event: {events}"
    )


def test_serve_without_monitor_does_not_sweep(cp_engine, monkeypatch) -> None:
    """NEGATIVE CONTROL: with start_monitor=False (the pre-fix state / the default
    create_app used by tests) NO sweeper is wired, so the same stale run stays
    RUNNING. This proves the positive test passes because of the start_monitor
    gating — not vacuously (e.g. some other code path reaping the run)."""
    monkeypatch.setenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", "0.2")

    now = datetime.now(UTC)
    run_id = _insert_stale_run(cp_engine, created_at=now - timedelta(hours=1))

    app = create_app(
        engine=cp_engine,
        recorder=ProvenanceRecorder(make_session_factory(cp_engine)),
        start_monitor=False,
    )
    with TestClient(app):
        time.sleep(2.0)

    session_factory = make_session_factory(cp_engine)
    with session_factory() as session:
        run = session.execute(select(RunORM).where(RunORM.id == run_id)).scalars().one()
    assert run.status is RunStatus.RUNNING, (
        f"run should stay RUNNING with no sweeper wired; got {run.status}"
    )
