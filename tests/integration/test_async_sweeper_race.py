"""Cluster Z — RunStateSweeper vs concurrent run completion.

The sweeper has a classic ``read-state → conditional-write`` shape
without a CAS guard:

    1. SELECT runs WHERE status IN ('running', 'paused')
    2. for each run:
         compute "is stale?" from latest provenance event
         if stale:
            run.status = FAILED         # ORM dirty
            session.commit()            # UPDATE run SET status='failed' WHERE id=:id
            recorder.record(RUN_FAILED)

The UPDATE has NO ``WHERE status IN sweepable_states`` predicate.
If, between step 1 and the UPDATE in step 2, the run's real
executor commits ``RUN_COMPLETED`` + ``status=COMPLETED``, the
sweeper's UPDATE silently overwrites the COMPLETED transition
with FAILED.

This test forces the race deterministically using a SQLAlchemy
``do_orm_execute`` event hook to introduce a small wait between
the sweeper's candidate-read and its terminal UPDATE. During that
window, a separate thread commits the legitimate completion. The
bug shows up as the run ending in FAILED while a RUN_COMPLETED
provenance event is on the chain — i.e., a completed run was
re-marked as failed.

Fix direction: change the sweeper's UPDATE to a conditional
``UPDATE run SET status='failed' WHERE id=:id AND status IN
('running','paused')`` and observe ``rowcount`` to decide whether
to record the RUN_FAILED event. Lost-update goes away; both the
SQLite and Postgres backends honor ``UPDATE ... WHERE`` atomically.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.notifications.sweeper import RunStateSweeper
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import (
    ProvenanceEventType,
    RunStatus,
)
from sqlalchemy import event, text


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


def _insert_running_stale_run(engine, *, age: timedelta) -> UUID:
    run_id = uuid4()
    created_at = datetime.now(UTC) - age
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, :st, :ts)"
            ),
            {
                "id": str(run_id),
                "uid": "alex",
                # SQLAEnum(native_enum=False) stores enum NAMES (upper).
                "st": "RUNNING",
                "ts": created_at.isoformat(),
            },
        )
    return run_id


def test_sweeper_does_not_regress_completed_run(tmp_path) -> None:
    """A run that the real executor completes WHILE the sweeper is
    deciding whether to mark it stale must NOT end up regressed to
    FAILED.

    Final-state contract (any one of these is acceptable):
      A) status=COMPLETED, RUN_COMPLETED on chain, no RUN_FAILED
         (the completion won; sweeper saw the new status and skipped).
      B) status=FAILED,    RUN_FAILED   on chain, no RUN_COMPLETED
         (the sweeper won outright; completer never committed).

    Bug shape: status=FAILED with RUN_COMPLETED also present on the
    chain. That means the completer wrote RUN_COMPLETED + flipped
    status → COMPLETED, and then the sweeper's unconditional UPDATE
    overwrote it back to FAILED. The run lost its terminal outcome.
    """
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)
    sweeper = RunStateSweeper(session_factory, recorder)

    # Stale enough that the sweeper will absolutely flag it.
    run_id = _insert_running_stale_run(engine, age=timedelta(hours=2))

    sweeper_observed_run = threading.Event()
    completer_done = threading.Event()
    completer_error: list[BaseException] = []

    # SQLAlchemy event hook: when the sweeper's session executes the
    # candidate-runs SELECT, signal the completer thread, then sleep
    # briefly so the completer can commit RUN_COMPLETED before the
    # sweeper continues to its UPDATE.
    def on_orm_execute(orm_execute_state):
        try:
            sql = str(orm_execute_state.statement.compile(
                compile_kwargs={"literal_binds": True}
            ))
        except Exception:
            return
        if (
            "FROM run" in sql
            and "status IN" in sql
            and not sweeper_observed_run.is_set()
        ):
            sweeper_observed_run.set()
            # Give the completer thread a head start to commit its
            # COMPLETED transition before we continue to UPDATE.
            time.sleep(0.4)

    def complete_run() -> None:
        try:
            # Wait until the sweeper has read its candidates.
            if not sweeper_observed_run.wait(timeout=5):
                completer_error.append(RuntimeError("sweeper did not signal"))
                return
            now = datetime.now(UTC).isoformat()
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE run SET status='COMPLETED', "
                        "completed_at=:ts WHERE id=:id"
                    ),
                    {"ts": now, "id": str(run_id)},
                )
            recorder.record(
                run_id=run_id,
                event_type=ProvenanceEventType.RUN_COMPLETED,
                actor="real_executor",
                payload={"by": "race-thread"},
            )
        except BaseException as exc:  # noqa: BLE001 - reraise from main
            completer_error.append(exc)
        finally:
            completer_done.set()

    completer_thread = threading.Thread(target=complete_run)
    completer_thread.start()

    # Listen on every session created by the factory we pass to the
    # sweeper — narrowly scoped so we don't perturb the recorder's
    # own sessions.
    sweep_session_factory = make_session_factory(engine)

    def _attach_listener(session, _transaction, _connection):
        # Only listen on sessions used by the sweeper.
        if not getattr(session, "_sweeper_marker", False):
            return

    # Approach: attach the listener at Session class level but only
    # honor the wait when we see the candidate-runs SELECT shape.
    from sqlalchemy.orm import Session as ORMSession

    event.listen(ORMSession, "do_orm_execute", on_orm_execute)
    try:
        sweep_sweeper = RunStateSweeper(sweep_session_factory, recorder)
        sweep_results = sweep_sweeper.sweep()
    finally:
        event.remove(ORMSession, "do_orm_execute", on_orm_execute)

    completer_thread.join(timeout=10)
    assert not completer_error, f"completer thread crashed: {completer_error[0]!r}"
    assert completer_done.is_set(), "completer thread did not finish"
    assert sweeper_observed_run.is_set(), (
        "sweeper SELECT was never observed by the event hook — the "
        "test is not actually exercising the race window"
    )

    # Inspect final state. Use a raw sqlite3 connection so we bypass
    # any SQLAlchemy connection-pool / snapshot caching that might
    # serve a stale view in this process. Note: SQLAEnum(native=False)
    # stores enum NAMES, so the literals are uppercase here.
    import sqlite3
    db_path = str(tmp_path / "cp.db")
    engine.dispose()
    raw = sqlite3.connect(db_path)
    final_status = raw.execute(
        "SELECT status FROM run WHERE id=?", (str(run_id),)
    ).fetchone()[0]
    completed_count = raw.execute(
        "SELECT COUNT(*) FROM provenance_event "
        "WHERE run_id=? AND event_type='RUN_COMPLETED'",
        (str(run_id),),
    ).fetchone()[0]
    failed_count = raw.execute(
        "SELECT COUNT(*) FROM provenance_event "
        "WHERE run_id=? AND event_type='RUN_FAILED'",
        (str(run_id),),
    ).fetchone()[0]
    raw.close()

    # Diagnostic: print so the failure mode is legible.
    print(
        f"\n[sweeper-race] sweep_results={len(sweep_results)} "
        f"final_status={final_status} "
        f"run_completed_events={completed_count} "
        f"run_failed_events={failed_count}"
    )

    # Acceptable final shapes (one terminal outcome only):
    if final_status == "COMPLETED":
        assert completed_count == 1, (
            f"COMPLETED status with {completed_count} RUN_COMPLETED events"
        )
        assert failed_count == 0, (
            f"COMPLETED status but {failed_count} RUN_FAILED events on chain — "
            "sweeper recorded a phantom failure for a completed run"
        )
    elif final_status == "FAILED":
        # If sweeper won the race entirely (completer never wrote),
        # this is fine: status FAILED + 1 RUN_FAILED + 0 RUN_COMPLETED.
        # If completer wrote RUN_COMPLETED first and sweeper THEN
        # overwrote status → FAILED, we have completed_count==1 too.
        # That is the bug.
        assert completed_count == 0, (
            f"BUG: status={final_status} but RUN_COMPLETED is on the chain "
            f"({completed_count}). The real executor's completion was "
            "silently overwritten by the sweeper. Fix direction: make "
            "the sweeper's status UPDATE conditional on "
            "status IN ('RUNNING','PAUSED')."
        )
        assert failed_count == 1, (
            f"FAILED status with {failed_count} RUN_FAILED events"
        )
    else:
        pytest.fail(f"unexpected final status: {final_status}")


def test_two_concurrent_sweepers_do_not_double_record_run_failed(tmp_path) -> None:
    """Two ``sweep()`` calls running concurrently against the same
    stale run must not both emit RUN_FAILED for it.

    The sweeper's status update is idempotent (status=FAILED →
    status=FAILED is a no-op semantically), but each sweep that
    *thinks* it transitioned the run also records a RUN_FAILED
    provenance event. Two RUN_FAILED entries on the same chain is a
    real provenance bug — there is exactly one transition out of
    RUNNING, so there should be exactly one terminal event.

    Fix direction: same conditional UPDATE WHERE status IN
    sweepable_states. ``rowcount`` then tells you whether THIS
    sweeper actually performed the transition; only emit RUN_FAILED
    if rowcount == 1.
    """
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)

    run_id = _insert_running_stale_run(engine, age=timedelta(hours=2))

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run_sweep() -> None:
        try:
            local_factory = make_session_factory(engine)
            sweeper = RunStateSweeper(local_factory, recorder)
            barrier.wait(timeout=5)
            sweeper.sweep()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=run_sweep)
    t2 = threading.Thread(target=run_sweep)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"sweeper thread crashed: {errors[0]!r}"

    import sqlite3
    db_path = str(tmp_path / "cp.db")
    engine.dispose()
    raw = sqlite3.connect(db_path)
    failed_count = raw.execute(
        "SELECT COUNT(*) FROM provenance_event "
        "WHERE run_id=? AND event_type='RUN_FAILED'",
        (str(run_id),),
    ).fetchone()[0]
    final_status = raw.execute(
        "SELECT status FROM run WHERE id=?", (str(run_id),)
    ).fetchone()[0]
    raw.close()

    print(
        f"\n[sweeper-vs-sweeper] final_status={final_status} "
        f"run_failed_events={failed_count}"
    )

    assert final_status == "FAILED", (
        f"both sweepers ran on a stale RUNNING run; expected status=FAILED, "
        f"got {final_status}"
    )
    assert failed_count == 1, (
        f"BUG: two concurrent sweepers each emitted a RUN_FAILED event "
        f"({failed_count} on the chain). The sweeper's UPDATE is "
        "unconditional and rowcount-blind, so both 'transitions' "
        "look successful. Fix: conditional UPDATE WHERE status IN "
        "sweepable_states; only record RUN_FAILED if rowcount == 1."
    )
