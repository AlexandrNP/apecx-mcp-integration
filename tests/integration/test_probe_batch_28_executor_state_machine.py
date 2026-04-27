"""Probe batch 28 — LocalExecutor task-state machine
(probes 730-754).

The LocalExecutor is the truth-teller for run terminal status:
its ``ExecutionResult`` is what the MCP layer returns to scientists.
Cluster AJ (2026-04-26) was the bug class where the executor
fabricated COMPLETED even when another writer (sweeper, /cancel)
had already terminated the run; the fix made ``_mark_completed`` /
``_mark_failed`` return bool and routed the result through
``_terminal_result`` which reads the actual DB status.

This batch pins those invariants so a future refactor can't silently
re-introduce the cluster AJ class:

  - ExecutionResult dataclass shape (frozen, kw_only)
  - Source-state sets (which states allow transition to terminal)
  - _validate_and_fetch error paths (run absent / artifact absent /
    yaml absent)
  - _mark_completed / _mark_failed conditional UPDATE truth bool
  - _terminal_result honors actual DB status when
    transitioned=False
  - _read_actual_status returns RunStatus or None
  - execute(unknown_run) returns FAILED with "not found" reason
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path):
    """A real migrated SQLite DB + session factory."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import (
        make_engine, make_session_factory,
    )
    p = tmp_path / "exec.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{p}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{p}")
    return eng, make_session_factory(eng)


@pytest.fixture
def executor(db, tmp_path):
    """A real LocalExecutor with a stub artifact_store."""
    from apecx_integration.control_plane.executors.local import LocalExecutor
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    eng, sf = db
    recorder = ProvenanceRecorder(sf)
    return LocalExecutor(
        session_factory=sf,
        artifact_store=MagicMock(),
        recorder=recorder,
        workflow_base_dir=tmp_path,
    )


def _insert_run(session_factory, **fields) -> uuid.UUID:
    """Insert a Run row and return its id."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    rid = uuid.uuid4()
    with session_factory() as session:
        run = Run(
            id=rid, user_id="u",
            status=fields.get("status", RunStatus.PENDING),
            created_at=datetime.now(UTC),
            workflow_config_id=fields.get("workflow_config_id"),
        )
        session.add(run)
        session.commit()
    return rid


# ---------------------------------------------------------------------------
# ExecutionResult dataclass invariants — probes 730-733
# ---------------------------------------------------------------------------


def test_probe_730_execution_result_is_frozen() -> None:
    """ExecutionResult is the executor's truth-statement to the
    HTTP route. Mutating it post-construction would let a caller
    silently amend a result — that's how cluster AJ-class bugs
    sneak in."""
    from dataclasses import FrozenInstanceError
    from apecx_integration.control_plane.executors.local import (
        ExecutionResult,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    r = ExecutionResult(
        run_id=uuid.uuid4(),
        status=RunStatus.COMPLETED,
        reason=None,
        output_artifact_id=uuid.uuid4(),
    )
    with pytest.raises(FrozenInstanceError):
        r.status = RunStatus.FAILED  # type: ignore[misc]


def test_probe_731_execution_result_kw_only() -> None:
    """All fields must be kw_only — protects against positional
    argument ordering mistakes that would silently pass an
    artifact_id where the status was expected."""
    from apecx_integration.control_plane.executors.local import (
        ExecutionResult,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    with pytest.raises(TypeError):
        ExecutionResult(  # type: ignore[misc]
            uuid.uuid4(),
            RunStatus.COMPLETED,
            None,
            uuid.uuid4(),
        )


def test_probe_732_execution_result_completed_with_artifact() -> None:
    """COMPLETED runs MUST have output_artifact_id; FAILED runs
    MUST have None. Lock the convention via type hints + factory."""
    from apecx_integration.control_plane.executors.local import (
        ExecutionResult,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    aid = uuid.uuid4()
    r = ExecutionResult(
        run_id=uuid.uuid4(),
        status=RunStatus.COMPLETED,
        reason=None,
        output_artifact_id=aid,
    )
    assert r.output_artifact_id == aid
    assert r.reason is None


def test_probe_733_execution_result_failed_with_reason() -> None:
    """FAILED runs MUST have a reason string. ``reason=None`` on a
    FAILED status would mean "no explanation", which the MCP layer
    can't surface to the user."""
    from apecx_integration.control_plane.executors.local import (
        ExecutionResult,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    r = ExecutionResult(
        run_id=uuid.uuid4(),
        status=RunStatus.FAILED,
        reason="explicit failure reason",
        output_artifact_id=None,
    )
    assert r.status is RunStatus.FAILED
    assert r.reason == "explicit failure reason"


# ---------------------------------------------------------------------------
# Source state sets — probes 734-737
# ---------------------------------------------------------------------------


def test_probe_734_completed_source_states_locked() -> None:
    """A run can only transition to COMPLETED from RUNNING / PAUSED.
    Adding PENDING here would let a never-started run claim
    completion. Adding terminal states would re-birth dead runs."""
    from apecx_integration.control_plane.executors.local import LocalExecutor
    from apecx_integration.control_plane.schemas.enums import RunStatus
    assert LocalExecutor._COMPLETED_SOURCE_STATES == (
        RunStatus.RUNNING, RunStatus.PAUSED,
    )


def test_probe_735_failed_source_states_includes_pending() -> None:
    """_mark_failed accepts PENDING because validation failures
    (no workflow_config_id, missing artifact) fire BEFORE the
    RUN_STARTED transition. Without PENDING here, those PENDING
    runs would sit as orphans forever."""
    from apecx_integration.control_plane.executors.local import LocalExecutor
    from apecx_integration.control_plane.schemas.enums import RunStatus
    assert LocalExecutor._FAILED_SOURCE_STATES == (
        RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED,
    )


def test_probe_736_completed_states_subset_of_failed_states() -> None:
    """Any state that can transition to COMPLETED must also be
    able to transition to FAILED — failures during execution are
    always allowed."""
    from apecx_integration.control_plane.executors.local import LocalExecutor
    assert set(LocalExecutor._COMPLETED_SOURCE_STATES) <= set(
        LocalExecutor._FAILED_SOURCE_STATES
    )


def test_probe_737_terminal_states_not_in_source_sets() -> None:
    """Terminal states must NOT be source states for re-transition.
    A COMPLETED run cannot transition to anything else; same for
    FAILED, CANCELLED. This is the cluster AJ guard."""
    from apecx_integration.control_plane.executors.local import LocalExecutor
    from apecx_integration.control_plane.schemas.enums import RunStatus
    terminal = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    src_completed = set(LocalExecutor._COMPLETED_SOURCE_STATES)
    src_failed = set(LocalExecutor._FAILED_SOURCE_STATES)
    assert terminal.isdisjoint(src_completed)
    assert terminal.isdisjoint(src_failed)


# ---------------------------------------------------------------------------
# execute(unknown_run) — probes 738-740
# ---------------------------------------------------------------------------


def test_probe_738_execute_unknown_run_returns_failed(executor) -> None:
    from apecx_integration.control_plane.schemas.enums import RunStatus
    rid = uuid.uuid4()  # never inserted
    result = asyncio.run(executor.execute(rid))
    assert result.status is RunStatus.FAILED
    assert result.run_id == rid


def test_probe_739_execute_unknown_run_reason_says_not_found(executor) -> None:
    """The reason must explicitly say 'not found in DB' so the
    HTTP route can map it to 404-equivalent. A vague 'failed' is
    ambiguous between "ran and failed" and "couldn't find it"."""
    rid = uuid.uuid4()
    result = asyncio.run(executor.execute(rid))
    assert "not found" in result.reason
    assert str(rid) in result.reason


def test_probe_740_execute_unknown_run_no_artifact(executor) -> None:
    rid = uuid.uuid4()
    result = asyncio.run(executor.execute(rid))
    assert result.output_artifact_id is None


# ---------------------------------------------------------------------------
# _validate_and_fetch error paths — probes 741-744
# ---------------------------------------------------------------------------


def test_probe_741_validate_returns_none_for_missing_run(executor, db) -> None:
    rid = uuid.uuid4()
    yaml_path = executor._validate_and_fetch(rid)
    assert yaml_path is None


def test_probe_742_no_workflow_config_id_marks_failed(executor, db) -> None:
    """Run with workflow_config_id=None must be marked FAILED with
    failure_class='workflow_misconfigured'."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(sf)  # workflow_config_id defaults to None
    yaml_path = executor._validate_and_fetch(rid)
    assert yaml_path is None
    # Run must now be FAILED
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.FAILED


def test_probe_743_missing_artifact_row_marks_failed(executor, db) -> None:
    """Run with workflow_config_id pointing at a no-longer-present
    Artifact row → FAILED. The FK normally prevents this state on
    insert; we toggle PRAGMA foreign_keys=OFF for this probe so we
    can reproduce the defensive code path the executor guards."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    from sqlalchemy import text
    eng, sf = db
    fake_artifact_id = uuid.uuid4()
    # The FK is normally on. Disable it for this connection so the
    # invalid-FK insert succeeds, simulating "artifact row was
    # deleted out from under a run."
    with sf() as session:
        session.execute(text("PRAGMA foreign_keys = OFF"))
        rid = uuid.uuid4()
        run = Run(
            id=rid, user_id="u",
            status=RunStatus.PENDING,
            workflow_config_id=fake_artifact_id,
            created_at=datetime.now(UTC),
        )
        session.add(run)
        session.commit()
    yaml_path = executor._validate_and_fetch(rid)
    assert yaml_path is None
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.FAILED


def test_probe_744_artifact_file_missing_marks_failed(executor, db, tmp_path) -> None:
    """Artifact row exists but its on-disk location is missing →
    FAILED. This is the cluster AB-adjacent guard."""
    from apecx_integration.control_plane.models.entities import Artifact, Run
    from apecx_integration.control_plane.schemas.enums import (
        ArtifactKind, RunStatus,
    )
    _, sf = db
    rid = uuid.uuid4()
    aid = uuid.uuid4()
    nonexistent = tmp_path / "missing.yml"  # never created
    # Insert in FK-respecting order: Run first (with NULL FK), then
    # Artifact (with valid run_id), then update Run to point at it.
    with sf() as session:
        run = Run(
            id=rid, user_id="u",
            status=RunStatus.PENDING,
            workflow_config_id=None,
            created_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        artifact = Artifact(
            id=aid, run_id=rid,
            kind=ArtifactKind.GENERATED_WORKFLOW,
            location=str(nonexistent),
            content_hash="0" * 64,
            size_bytes=0,
            mime_type="application/yaml",
            created_at=datetime.now(UTC),
        )
        session.add(artifact)
        session.flush()
        run.workflow_config_id = aid
        session.commit()
    yaml_path = executor._validate_and_fetch(rid)
    assert yaml_path is None
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.FAILED


# ---------------------------------------------------------------------------
# _mark_completed / _mark_failed truth bool — probes 745-751
# ---------------------------------------------------------------------------


def test_probe_745_mark_completed_returns_true_for_running_run(
    executor, db,
) -> None:
    """Cluster AJ — _mark_completed must return True iff this call
    actually drove the transition."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(sf, status=RunStatus.RUNNING)
    aid = uuid.uuid4()
    transitioned = executor._mark_completed(rid, aid)
    assert transitioned is True
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.COMPLETED


def test_probe_746_mark_completed_returns_false_for_terminal(
    executor, db,
) -> None:
    """Cluster AJ — if the run is already terminal (e.g. swept to
    FAILED), _mark_completed must return False, NOT pretend to
    have completed it."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(sf, status=RunStatus.FAILED)
    transitioned = executor._mark_completed(rid, uuid.uuid4())
    assert transitioned is False
    with sf() as session:
        run = session.get(Run, rid)
        # Status must NOT have been overwritten to COMPLETED
        assert run.status is RunStatus.FAILED


def test_probe_747_mark_completed_emits_provenance(executor, db) -> None:
    """A successful _mark_completed must emit RUN_COMPLETED.
    A failed (rowcount=0) call must NOT emit it — silently
    appending a RUN_COMPLETED to a FAILED run corrupts the chain."""
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent, Run,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType, RunStatus,
    )
    from sqlalchemy import select
    _, sf = db
    # Successful transition path
    rid_ok = _insert_run(sf, status=RunStatus.RUNNING)
    executor._mark_completed(rid_ok, uuid.uuid4())
    # Already-terminal path — no event
    rid_no = _insert_run(sf, status=RunStatus.FAILED)
    executor._mark_completed(rid_no, uuid.uuid4())
    with sf() as session:
        events_ok = session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.run_id == rid_ok,
                ProvenanceEvent.event_type == ProvenanceEventType.RUN_COMPLETED,
            )
        ).scalars().all()
        events_no = session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.run_id == rid_no,
                ProvenanceEvent.event_type == ProvenanceEventType.RUN_COMPLETED,
            )
        ).scalars().all()
    assert len(events_ok) == 1
    assert len(events_no) == 0


def test_probe_748_mark_failed_in_session_pending_returns_true(
    executor, db,
) -> None:
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(sf, status=RunStatus.PENDING)
    with sf() as session:
        transitioned = executor._mark_failed(
            rid, "test reason",
            failure_class="test_class",
            in_session=session,
        )
        session.commit()
    assert transitioned is True
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.FAILED


def test_probe_749_mark_failed_in_session_terminal_returns_false(
    executor, db,
) -> None:
    """Cluster AJ — _mark_failed in_session path on a terminal run
    must return False without touching status."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(sf, status=RunStatus.COMPLETED)
    with sf() as session:
        transitioned = executor._mark_failed(
            rid, "shouldn't apply",
            failure_class="test_class",
            in_session=session,
        )
        session.commit()
    assert transitioned is False
    with sf() as session:
        run = session.get(Run, rid)
        # Status must not change
        assert run.status is RunStatus.COMPLETED


def test_probe_750_mark_failed_post_started_terminal_returns_false(
    executor, db,
) -> None:
    """Cluster AJ — _mark_failed without in_session (post
    RUN_STARTED) on a terminal run must return False."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(sf, status=RunStatus.CANCELLED)
    transitioned = executor._mark_failed(
        rid, "would re-birth", failure_class="x",
    )
    assert transitioned is False
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.CANCELLED


def test_probe_751_mark_failed_emits_run_failed_with_reason(
    executor, db,
) -> None:
    from apecx_integration.control_plane.models.entities import ProvenanceEvent
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType, RunStatus,
    )
    from sqlalchemy import select
    _, sf = db
    rid = _insert_run(sf, status=RunStatus.RUNNING)
    transitioned = executor._mark_failed(
        rid, "specific reason",
        failure_class="executor_crashed",
    )
    assert transitioned is True
    with sf() as session:
        ev = session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.run_id == rid,
                ProvenanceEvent.event_type == ProvenanceEventType.RUN_FAILED,
            )
        ).scalar_one()
        payload = ev.payload
        assert payload["reason"] == "specific reason"
        assert payload["failure_class"] == "executor_crashed"


# ---------------------------------------------------------------------------
# _read_actual_status + _terminal_result — probes 752-754
# ---------------------------------------------------------------------------


def test_probe_752_read_actual_status_returns_status_or_none(
    executor, db,
) -> None:
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    # Existent run
    rid = _insert_run(sf, status=RunStatus.RUNNING)
    assert executor._read_actual_status(rid) is RunStatus.RUNNING
    # Non-existent run
    assert executor._read_actual_status(uuid.uuid4()) is None


def test_probe_753_terminal_result_transitioned_true_returns_intended(
    executor,
) -> None:
    """When this executor drove the transition, ExecutionResult
    must report the intended status verbatim."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    rid = uuid.uuid4()
    aid = uuid.uuid4()
    result = executor._terminal_result(
        run_id=rid,
        intended_status=RunStatus.COMPLETED,
        transitioned=True,
        intended_reason=None,
        output_artifact_id=aid,
    )
    assert result.status is RunStatus.COMPLETED
    assert result.reason is None
    assert result.output_artifact_id == aid


def test_probe_754_terminal_result_transitioned_false_reads_actual(
    executor, db,
) -> None:
    """Cluster AJ regression — when transitioned=False (another
    writer owned the terminal transition), the result MUST report
    the ACTUAL DB status, not the intended one. The reason field
    must explain the race."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(sf, status=RunStatus.FAILED)  # already terminal
    result = executor._terminal_result(
        run_id=rid,
        intended_status=RunStatus.COMPLETED,
        transitioned=False,
        intended_reason="executor_finished",
        output_artifact_id=None,
    )
    assert result.status is RunStatus.FAILED  # actual, not intended
    assert "another writer" in result.reason
    assert "FAILED" in result.reason or "failed" in result.reason
