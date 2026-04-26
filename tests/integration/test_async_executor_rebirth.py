"""Cluster AB — LocalExecutor rebirths runs from terminal states.

``LocalExecutor._mark_completed`` and ``_mark_failed`` both write
status unconditionally:

    run = session.get(RunORM, run_id)
    run.status = RunStatus.COMPLETED  # or FAILED
    run.completed_at = now
    session.commit()

If the run is ALREADY in a terminal state (COMPLETED, FAILED, or
CANCELLED), the executor will silently rebirth it. The realistic
trigger:

  1. /workflows/execute starts; RUN_STARTED recorded; workflow
     runs slowly (or stalls).
  2. RunStateSweeper sees the run RUNNING with stale provenance;
     conditional UPDATE flips it to FAILED + records RUN_FAILED.
     (Cluster Z's fix made the sweeper conditional, but the
     sweeper still writes FAILED to a run that is going to
     finish later.)
  3. The slow workflow finally returns. ``_mark_completed`` runs
     and unconditionally OVERWRITES status FAILED → COMPLETED.
     Provenance now has RUN_FAILED followed by RUN_COMPLETED for
     the same run; the row says COMPLETED.

That's the same bug shape as cluster Z, but on the executor side.
The cluster Z fix protected the sweeper from reviving a COMPLETED
run; this protects the executor from reviving a FAILED run.

Symmetric on ``_mark_failed``: a workflow that fails late, after
the run was previously CANCELLED or COMPLETED externally, would
rebirth COMPLETED → FAILED.

Fix: conditional UPDATE.

    update(Run).where(Run.id == run_id)
               .where(Run.status.in_({RUNNING, PAUSED}))
               .values(status=COMPLETED, completed_at=now)
    if rowcount == 0:
        # Don't record terminal event — somebody else already did.

Same shape as Z (sweeper) and AA (revoke). The
read-state→write-state-without-CAS pattern keeps recurring; every
time we find one, the cure is the same.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.executors.local import LocalExecutor
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from sqlalchemy import text


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


def _seed_run(engine, *, status_value: str) -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, "
                "completed_at) VALUES (:id, 'alex', :st, :ts, :ts)"
            ),
            {"id": str(run_id), "st": status_value, "ts": now},
        )
    return run_id


@pytest.fixture
def executor_rig(tmp_path):
    engine = _migrated_engine(tmp_path)
    factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(factory)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(exist_ok=True)
    store = ArtifactStore(
        session_factory=factory,
        recorder=recorder,
        root=artifact_root,
    )
    executor = LocalExecutor(
        session_factory=factory,
        artifact_store=store,
        recorder=recorder,
        workflow_base_dir=tmp_path,
    )
    return engine, factory, recorder, executor


def test_mark_completed_does_not_rebirth_failed_run(executor_rig) -> None:
    """A run already marked FAILED (e.g. by the sweeper) must not
    be silently flipped to COMPLETED when the executor's slow
    workflow finally returns.

    This is the cluster-Z scenario inverted: there the sweeper was
    the unconditional writer; here it's the executor.
    """
    engine, _factory, _recorder, executor = executor_rig

    # Pre-condition: sweeper has already terminated this run.
    run_id = _seed_run(engine, status_value="FAILED")
    output_artifact_id = uuid4()  # Fake; _mark_completed doesn't validate it.

    # The slow workflow has finally returned and now the executor
    # is calling _mark_completed. Currently this is unconditional.
    executor._mark_completed(run_id, output_artifact_id)

    with engine.connect() as conn:
        final_status = conn.execute(
            text("SELECT status FROM run WHERE id=:id"),
            {"id": str(run_id)},
        ).scalar_one()

    print(f"\n[executor-rebirth-completed] final_status={final_status}")
    assert final_status == "FAILED", (
        f"BUG: LocalExecutor._mark_completed rebirthed a FAILED run "
        f"to {final_status}. The sweeper-or-other terminal "
        "transition was silently overwritten. Fix: conditional UPDATE "
        "WHERE status IN (RUNNING, PAUSED)."
    )


def test_mark_failed_does_not_rebirth_completed_run(executor_rig) -> None:
    """A run already marked COMPLETED (somehow — externally cancelled,
    earlier completer, etc.) must not be silently flipped to FAILED
    when a late executor error fires _mark_failed.
    """
    engine, _factory, _recorder, executor = executor_rig

    run_id = _seed_run(engine, status_value="COMPLETED")
    executor._mark_failed(
        run_id, "late-executor-error", failure_class="execute_failed"
    )

    with engine.connect() as conn:
        final_status = conn.execute(
            text("SELECT status FROM run WHERE id=:id"),
            {"id": str(run_id)},
        ).scalar_one()

    print(f"\n[executor-rebirth-failed] final_status={final_status}")
    assert final_status == "COMPLETED", (
        f"BUG: LocalExecutor._mark_failed rebirthed a COMPLETED run "
        f"to {final_status}. A previous terminal transition was "
        "silently overwritten. Fix: conditional UPDATE WHERE "
        "status IN (RUNNING, PAUSED)."
    )


def test_mark_failed_does_not_rebirth_cancelled_run(executor_rig) -> None:
    """Same shape: a run previously CANCELLED (e.g. by a future
    /workflows/cancel route) must not be re-FAILED by a late
    executor error.

    Even though /workflows/cancel does not exist today, the contract
    is that CANCELLED is terminal. The executor must respect it.
    """
    engine, _factory, _recorder, executor = executor_rig

    run_id = _seed_run(engine, status_value="CANCELLED")
    executor._mark_failed(
        run_id, "late-executor-error", failure_class="execute_failed"
    )

    with engine.connect() as conn:
        final_status = conn.execute(
            text("SELECT status FROM run WHERE id=:id"),
            {"id": str(run_id)},
        ).scalar_one()

    print(f"\n[executor-rebirth-cancelled] final_status={final_status}")
    assert final_status == "CANCELLED", (
        f"BUG: LocalExecutor._mark_failed rebirthed a CANCELLED run "
        f"to {final_status}. CANCELLED is a terminal state and must "
        "not be overwritten by a late executor."
    )
