"""T08 integration: RunStateSweeper against a real migrated SQLite DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.notifications.sweeper import RunStateSweeper
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import (
    ProvenanceEventType,
    RunStatus,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrated_engine(tmp_path):
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return make_engine(url)


def _insert_run(engine, *, status_value: str, created_at: datetime, user_id: str = "alex") -> UUID:
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, :st, :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "st": status_value, "ts": created_at.isoformat()},
        )
    return run_id


@pytest.fixture
def rig(tmp_path):
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)
    sweeper = RunStateSweeper(session_factory, recorder)
    return engine, recorder, sweeper


def test_fresh_running_run_not_swept(rig) -> None:
    """A run in RUNNING state with a provenance event just seconds
    old must NOT be swept — this is the "legitimately active" case.
    """
    engine, recorder, sweeper = rig
    now = datetime.now(UTC)
    run_id = _insert_run(engine, status_value="RUNNING", created_at=now - timedelta(seconds=5))
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="test",
        payload={},
        now=now - timedelta(seconds=2),
    )
    results = sweeper.sweep(stale_after=timedelta(minutes=15), now=now)
    assert results == []


def test_stale_running_run_swept_to_failed(rig) -> None:
    engine, recorder, sweeper = rig
    now = datetime.now(UTC)
    run_id = _insert_run(engine, status_value="RUNNING", created_at=now - timedelta(hours=2))
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="test",
        payload={},
        now=now - timedelta(hours=2),
    )
    results = sweeper.sweep(stale_after=timedelta(minutes=15), now=now)
    assert len(results) == 1
    r = results[0]
    assert r.run_id == run_id
    assert r.old_status is RunStatus.RUNNING
    assert r.new_status is RunStatus.FAILED
    assert "sweeping to FAILED" in r.reason

    # DB state reflects the sweep.
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM run WHERE id = :id"), {"id": str(run_id)}
        ).scalar()
    assert row == "FAILED"


def test_stale_paused_run_swept(rig) -> None:
    """PAUSED runs that go silent also get swept — a paused run whose
    HITL approver never decides is as abandoned as a crashed one.
    """
    engine, recorder, sweeper = rig
    now = datetime.now(UTC)
    run_id = _insert_run(engine, status_value="PAUSED", created_at=now - timedelta(hours=1))
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.STEP_STARTED,
        actor="test",
        payload={},
        now=now - timedelta(hours=1),
    )
    results = sweeper.sweep(stale_after=timedelta(minutes=15), now=now)
    assert len(results) == 1
    assert results[0].old_status is RunStatus.PAUSED


def test_pending_and_completed_runs_never_swept(rig) -> None:
    """PENDING hasn't started; COMPLETED is a terminal state. Neither
    should ever be touched by the sweeper — it operates only on
    active states.
    """
    engine, _recorder, sweeper = rig
    now = datetime.now(UTC)
    _insert_run(engine, status_value="PENDING", created_at=now - timedelta(days=1))
    _insert_run(engine, status_value="COMPLETED", created_at=now - timedelta(days=1))
    _insert_run(engine, status_value="FAILED", created_at=now - timedelta(days=1))
    results = sweeper.sweep(stale_after=timedelta(minutes=15), now=now)
    assert results == []


def test_run_with_no_provenance_but_recent_created_at_not_swept(rig) -> None:
    """A run that just got created a second ago shouldn't be swept
    just because it hasn't emitted a provenance event yet — the
    workflow may still be spinning up. We use created_at as the
    fallback age when no events exist.
    """
    engine, _recorder, sweeper = rig
    now = datetime.now(UTC)
    _insert_run(engine, status_value="RUNNING", created_at=now - timedelta(seconds=5))
    results = sweeper.sweep(stale_after=timedelta(minutes=15), now=now)
    assert results == []


def test_sweep_records_run_failed_provenance_event(rig) -> None:
    """The sweep action itself is a provenance event — the hash chain
    records *why* the run was marked FAILED.
    """
    engine, _recorder, sweeper = rig
    now = datetime.now(UTC)
    run_id = _insert_run(engine, status_value="RUNNING", created_at=now - timedelta(hours=2))
    sweeper.sweep(stale_after=timedelta(minutes=15), now=now)

    with engine.connect() as conn:
        events = conn.execute(
            text(
                "SELECT event_type, actor FROM provenance_event "
                "WHERE run_id = :rid ORDER BY timestamp"
            ),
            {"rid": str(run_id)},
        ).all()
    # There's at least one RUN_FAILED written by the sweeper (actor = run_state_sweeper).
    assert any(
        e[0] == "RUN_FAILED" and e[1] == "run_state_sweeper" for e in events
    ), events


def test_sweep_threshold_boundary_is_strict(rig) -> None:
    """A run whose last event is exactly at the boundary is NOT
    swept — the comparison is ``>=`` against ``cutoff``.
    """
    engine, recorder, sweeper = rig
    now = datetime.now(UTC)
    stale_after = timedelta(minutes=15)
    run_id = _insert_run(engine, status_value="RUNNING", created_at=now - timedelta(minutes=30))
    # Last event exactly at the cutoff -> not swept.
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.STEP_STARTED,
        actor="test",
        payload={},
        now=now - stale_after,  # exactly at the cutoff boundary
    )
    results = sweeper.sweep(stale_after=stale_after, now=now)
    assert results == []
