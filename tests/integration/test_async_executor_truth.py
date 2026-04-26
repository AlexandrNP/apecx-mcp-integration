"""Cluster AJ — LocalExecutor.execute() lies about run status.

Cluster AB made ``_mark_completed`` and ``_mark_failed``
conditional UPDATEs that skip the transition + the terminal
provenance event when ``rowcount == 0`` (i.e. the run is no
longer in RUNNING/PAUSED — likely because the sweeper marked it
FAILED first, or a future /workflows/cancel transitioned it to
CANCELLED).

That's the right behavior for the WRITER side. But ``execute()``
unconditionally returns ``ExecutionResult(status=RunStatus.COMPLETED, ...)``
after calling ``_mark_completed``. SILENT FAILURE: the helper
correctly skipped the transition because somebody else owns the
terminal state, but the executor still reports "I completed
your workflow" to the caller. The HTTP response on
``/workflows/execute`` then claims success. The user / MCP tool
sees "completed" and moves on, never noticing that:
  - the run was actually FAILED in the DB,
  - the OUTPUT artifact was persisted but is detached from the
    run's terminal state,
  - the audit chain has no RUN_COMPLETED event for this run.

Same lie shape applies to the ``_mark_failed`` paths: if the
run was already terminal, ``_mark_failed`` skips, but the
caller returns ``status=FAILED``.

Fix (fail-fast): ``_mark_completed`` and ``_mark_failed`` return
a bool indicating whether they actually performed the
transition. ``execute()`` consults the return value; on False it
reads the actual current status from the DB and returns THAT in
the ExecutionResult, with a non-None ``reason`` explaining why
the executor's transition was rejected.

This file probes the COMPLETED-LIES case:
  1. Insert a run that's eligible to be executed (status=RUNNING,
     workflow_config_id set).
  2. Pre-flip the run to FAILED in the DB (simulates the
     sweeper, or any external terminal transition).
  3. Stub the executor's workflow loader/runner to return a
     fake successful result (no nanobrain needed — we're
     testing the post-_mark_completed path).
  4. Call executor.execute(run_id).
  5. Assert: result.status == FAILED (truth), NOT COMPLETED
     (lie). result.reason is non-None and explains the
     situation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.executors.local import (
    ExecutionResult,
    LocalExecutor,
    run_sync,
)
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import RunStatus
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


def _seed_run_and_workflow_artifact(engine, tmp_path) -> tuple[UUID, UUID]:
    """Insert a Run + the GENERATED_WORKFLOW Artifact it points at.
    Writes a minimal valid YAML to disk so _validate_and_fetch
    succeeds. Returns (run_id, artifact_id).
    """
    run_id = uuid4()
    artifact_id = uuid4()
    yaml_path = tmp_path / "wf.yml"
    yaml_path.write_text(
        "name: probe_workflow\nsteps: {}\nlinks: {}\n", encoding="utf-8"
    )
    now = datetime.now(UTC).isoformat()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, "
                "'sha256-placeholder', 1, 'application/x-yaml', :ts)"
            ),
            {
                "id": str(artifact_id),
                "rid": str(run_id),
                "loc": str(yaml_path),
                "ts": now,
            },
        )
        conn.execute(
            text(
                "UPDATE run SET workflow_config_id = :aid WHERE id = :rid"
            ),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return run_id, artifact_id


@pytest.fixture
def stubbed_executor(tmp_path, monkeypatch):
    """A LocalExecutor whose workflow load + execute are stubbed to
    succeed without nanobrain. Lets us exercise the post-_mark_completed
    path on any host (no nanobrain install required).
    """
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

    # Stub the parts of execute() that need nanobrain. We replace
    # ``_stage_workflow`` to return the original yaml path
    # unchanged (no symlinks, no copy), and we replace the
    # ``Workflow.from_config`` import + ``workflow.process`` calls
    # by patching the local import inside execute.
    class _FakeWorkflow:
        @staticmethod
        def from_config(_path):
            return _FakeWorkflow()

        async def process(self, _input):
            return {"fake": "result"}

    import sys
    import types

    fake_module = types.ModuleType("nanobrain.core.workflow")
    fake_module.Workflow = _FakeWorkflow  # type: ignore[attr-defined]
    nanobrain_pkg = sys.modules.setdefault(
        "nanobrain", types.ModuleType("nanobrain")
    )
    nanobrain_core = sys.modules.setdefault(
        "nanobrain.core", types.ModuleType("nanobrain.core")
    )
    nanobrain_pkg.core = nanobrain_core  # type: ignore[attr-defined]
    nanobrain_core.workflow = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nanobrain", nanobrain_pkg)
    monkeypatch.setitem(sys.modules, "nanobrain.core", nanobrain_core)
    monkeypatch.setitem(sys.modules, "nanobrain.core.workflow", fake_module)

    return engine, executor


def test_execute_returns_actual_status_when_run_was_swept_failed(
    stubbed_executor, tmp_path
) -> None:
    """Pre-flip the run to FAILED before the executor finishes. The
    executor's _mark_completed will correctly skip, but execute()
    must NOT return status=COMPLETED. It must return the actual
    DB status (FAILED) with a reason explaining why.
    """
    engine, executor = stubbed_executor
    run_id, _artifact_id = _seed_run_and_workflow_artifact(engine, tmp_path)

    # Simulate a sweeper or external transition flipping the run
    # to FAILED while the executor's workflow.process is "running."
    # The cluster AB fix is at the ``_mark_completed`` level — the
    # transition will skip. Question: does execute() lie?
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE run SET status = 'FAILED', "
                "completed_at = :ts WHERE id = :id"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )

    result = run_sync(executor, run_id)

    # The actual DB status is FAILED. The executor must reflect
    # that, not lie about COMPLETED.
    with engine.connect() as conn:
        actual = conn.execute(
            text("SELECT status FROM run WHERE id = :id"),
            {"id": str(run_id)},
        ).scalar_one()

    print(
        f"\n[executor-truth] result.status={result.status} "
        f"actual_db_status={actual} reason={result.reason}"
    )
    assert actual == "FAILED", "DB sanity: pre-flip should have stuck"
    assert result.status is not RunStatus.COMPLETED, (
        f"BUG: executor.execute() returned status=COMPLETED for run "
        f"{run_id} but the actual DB status is {actual}. "
        "_mark_completed correctly skipped (cluster AB), but the "
        "caller fabricated a COMPLETED result. Silent failure: the "
        "HTTP route's response would lie to the user / MCP tool. "
        "Fix: _mark_completed returns bool; on False, execute() "
        "reads actual status and returns it with a reason."
    )
    assert result.status is RunStatus.FAILED, (
        f"executor.execute() should mirror the actual DB status "
        f"{actual}; got {result.status}"
    )
    assert result.reason, (
        "result.reason should be set so the caller knows why the "
        "executor's transition was rejected"
    )
