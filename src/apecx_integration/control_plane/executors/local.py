"""T01 P2 — local executor for composed workflows.

Takes a Run whose status is RUNNING (or PENDING auto-advanced), loads
the generated workflow YAML via ``nanobrain.core.workflow.Workflow.
from_config``, runs ``workflow.process({})``, persists the result as
an OUTPUT Artifact, and updates ``run.status`` + emits the
corresponding provenance event.

Failure contract
----------------
The executor's job is not to succeed — it is to fail *cleanly*. Three
failure modes are captured and marked ``RUN_FAILED`` with a specific
``reason`` payload in the provenance event:

- ``load_failed`` — the composed YAML doesn't load via
  ``Workflow.from_config``. Most commonly because a step's wrapper
  YAML path doesn't resolve, or because a class path references an
  uninstalled package.
- ``execute_failed`` — the workflow loaded but ``process()`` raised.
  Covers the "Ollama unreachable" / "BV-BRC snapshot missing" /
  "step raised" failure class.
- ``workflow_misconfigured`` — the Run state is invalid before we
  even try (no workflow_config_id, artifact on disk missing).

A successful run emits ``RUN_STARTED`` (before load) and
``RUN_COMPLETED`` (after persist). The provenance chain validates
for either path.

Workflow directory resolution
-----------------------------
The composer emits YAML with ``config: "steps/<name>.yml"`` relative
paths. Nanobrain's loader resolves these relative to the YAML file's
own directory. ``LocalExecutor`` stages a run-root directory that
symlinks the configured ``workflow_base_dir/steps`` into the temp
dir and copies the Artifact YAML in, so the relative paths resolve
against a real ``steps/`` tree without mutating the source tree.

Not in scope for P2
-------------------
- Mid-execution pausing for ``ApprovalStep`` (T10 already provides
  the step; wiring its run-state callbacks to the Control Plane is a
  separate closeout).
- Partial-result persistence per step (the AP §5.1 "HITL after first
  step" branch).
- Cancellation mid-run.

These are listed honestly so the next session doesn't rediscover
them the hard way.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.control_plane.models.entities import (
    Artifact as ArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    Run as RunORM,
)
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import (
    ArtifactKind,
    ProvenanceEventType,
    RunStatus,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ExecutionResult:
    run_id: UUID
    status: RunStatus  # COMPLETED or FAILED
    reason: str | None  # human-readable when FAILED; None on COMPLETED
    output_artifact_id: UUID | None  # None on FAILED


class LocalExecutor:
    """Local Tier-4 executor. Synchronous-looking API, async under the
    hood because nanobrain's ``Workflow.process`` is a coroutine."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        artifact_store: ArtifactStore,
        recorder: ProvenanceRecorder,
        workflow_base_dir: Path,
        actor: str = "local_executor",
    ) -> None:
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._recorder = recorder
        self._workflow_base_dir = Path(workflow_base_dir).resolve()
        self._actor = actor

    async def execute(self, run_id: UUID) -> ExecutionResult:
        yaml_path = self._validate_and_fetch(run_id)
        if yaml_path is None:
            # Precondition failure already marked FAILED inside helper.
            return ExecutionResult(
                run_id=run_id,
                status=RunStatus.FAILED,
                reason="workflow_misconfigured",
                output_artifact_id=None,
            )

        # Atomic claim. Two concurrent /workflows/execute calls on
        # the same run both pass _validate_and_fetch above (the helper
        # only validates state, doesn't claim). Without a guard here,
        # both proceed through workflow.process() to completion,
        # doubling every side effect. Migration 0002 added a partial
        # unique index on provenance_event(run_id) WHERE
        # event_type='RUN_STARTED'; the SECOND record() call here
        # raises IntegrityError, which we catch and short-circuit.
        # Found 2026-04-26 by adversarial test (cluster V3).
        from sqlalchemy.exc import IntegrityError

        try:
            self._recorder.record(
                run_id=run_id,
                event_type=ProvenanceEventType.RUN_STARTED,
                actor=self._actor,
                payload={
                    "workflow_yaml": str(yaml_path),
                    "workflow_base_dir": str(self._workflow_base_dir),
                },
            )
        except IntegrityError:
            log.warning(
                "Run %s already has a RUN_STARTED event; concurrent "
                "executor claimed it. Aborting without re-running.",
                run_id,
            )
            return ExecutionResult(
                run_id=run_id,
                status=RunStatus.RUNNING,
                reason="concurrent_executor_already_claimed_run",
                output_artifact_id=None,
            )

        import tempfile

        with tempfile.TemporaryDirectory(prefix="apecx_run_") as td:
            run_root = Path(td)
            staged_yaml = self._stage_workflow(yaml_path, run_root)

            try:
                # Lazy import — nanobrain is heavy; don't pay for it
                # in modules that only import LocalExecutor for typing.
                from nanobrain.core.workflow import Workflow
                workflow = Workflow.from_config(str(staged_yaml))
            except Exception as exc:
                reason = f"workflow load failed: {type(exc).__name__}: {exc}"
                log.warning("Run %s: %s", run_id, reason)
                self._mark_failed(run_id, reason, failure_class="load_failed")
                return ExecutionResult(
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    reason=reason,
                    output_artifact_id=None,
                )

            try:
                raw_result = await workflow.process({})
            except Exception as exc:
                reason = (
                    f"workflow execution failed: {type(exc).__name__}: {exc}"
                )
                log.warning("Run %s: %s", run_id, reason)
                self._mark_failed(
                    run_id, reason, failure_class="execute_failed"
                )
                return ExecutionResult(
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    reason=reason,
                    output_artifact_id=None,
                )

        output_artifact_id = self._persist_output(run_id, raw_result)
        self._mark_completed(run_id, output_artifact_id)

        return ExecutionResult(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            reason=None,
            output_artifact_id=output_artifact_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_and_fetch(self, run_id: UUID) -> Path | None:
        with self._session_factory() as session:
            run = session.get(RunORM, run_id)
            if run is None:
                log.warning("Run %s not found; skipping execution", run_id)
                return None
            if run.workflow_config_id is None:
                self._mark_failed(
                    run_id,
                    "run has no workflow_config_id",
                    failure_class="workflow_misconfigured",
                    in_session=session,
                )
                session.commit()
                return None
            artifact = session.get(ArtifactORM, run.workflow_config_id)
            if artifact is None:
                self._mark_failed(
                    run_id,
                    "workflow artifact row missing",
                    failure_class="workflow_misconfigured",
                    in_session=session,
                )
                session.commit()
                return None
            on_disk = Path(artifact.location)
            if not on_disk.is_file():
                self._mark_failed(
                    run_id,
                    f"workflow YAML not found on disk: {on_disk}",
                    failure_class="workflow_misconfigured",
                    in_session=session,
                )
                session.commit()
                return None
            return on_disk

    def _stage_workflow(self, yaml_path: Path, run_root: Path) -> Path:
        """Build a staging directory that nanobrain can load from.

        The composed YAML references ``steps/*.yml`` relative paths.
        We symlink the configured ``workflow_base_dir/steps`` into the
        staging dir and copy the YAML in, so relative resolution
        works without mutating the source tree.
        """
        steps_src = self._workflow_base_dir / "steps"
        if steps_src.is_dir():
            (run_root / "steps").symlink_to(steps_src, target_is_directory=True)
        staged = run_root / "workflow.yml"
        staged.write_bytes(yaml_path.read_bytes())
        return staged

    def _persist_output(self, run_id: UUID, raw_result: object) -> UUID:
        payload_bytes = json.dumps(
            raw_result, default=str, indent=2
        ).encode("utf-8")
        artifact = self._artifact_store.store(
            content=payload_bytes,
            kind=ArtifactKind.OUTPUT,
            run_id=run_id,
            mime_type="application/json",
        )
        return artifact.id

    # Conditional terminal-transition helpers (cluster AB,
    # 2026-04-26). The executor must not "rebirth" a run from a
    # terminal state. If the sweeper already marked the run FAILED
    # (cluster Z), or an external CANCELLED arrives, or any other
    # terminal transition has happened first, the executor's
    # _mark_completed / _mark_failed must be a no-op for the row
    # AND must skip emitting the terminal provenance event (the
    # winner already emitted theirs).
    #
    # The "legal source states" differ between the two helpers:
    # _mark_completed implies the executor actually ran the
    # workflow, so status must have been RUNNING/PAUSED. PENDING
    # → COMPLETED would be a bug (executor never started).
    # _mark_failed includes PENDING because validation failures
    # (no workflow_config_id, artifact missing on disk) fire
    # before the RUN_STARTED transition — we want those PENDING
    # runs marked FAILED so they don't sit as orphans.
    _COMPLETED_SOURCE_STATES = (RunStatus.RUNNING, RunStatus.PAUSED)
    _FAILED_SOURCE_STATES = (
        RunStatus.PENDING,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
    )

    def _mark_completed(
        self, run_id: UUID, output_artifact_id: UUID
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            result = session.execute(
                update(RunORM)
                .where(RunORM.id == run_id)
                .where(RunORM.status.in_(self._COMPLETED_SOURCE_STATES))
                .values(status=RunStatus.COMPLETED, completed_at=now)
            )
            session.commit()
            if result.rowcount == 0:
                log.warning(
                    "Run %s not in RUNNING/PAUSED at _mark_completed; "
                    "skipping terminal transition + RUN_COMPLETED event",
                    run_id,
                )
                return
        self._recorder.record(
            run_id=run_id,
            event_type=ProvenanceEventType.RUN_COMPLETED,
            actor=self._actor,
            payload={"output_artifact_id": str(output_artifact_id)},
        )

    def _mark_failed(
        self,
        run_id: UUID,
        reason: str,
        *,
        failure_class: str,
        in_session: Session | None = None,
    ) -> None:
        now = datetime.now(UTC)

        if in_session is not None:
            # Pre-RUN_STARTED validation-failure path. The run is
            # PENDING and no other writer touches it yet, so race
            # protection isn't relevant. Use the ORM-tracked
            # in-memory mutation so we don't issue a DML statement
            # that holds the SQLite writer lock and blocks the
            # recorder's separate session from committing the
            # RUN_FAILED event a few lines below. Caller commits.
            run = in_session.get(RunORM, run_id)
            if run is not None:
                run.status = RunStatus.FAILED
                run.completed_at = now
        else:
            # Post-RUN_STARTED failure path. The run was claimed
            # via the migration-0002 partial unique index, so its
            # status was RUNNING/PAUSED — until possibly the
            # sweeper or an external CANCELLED arrived. Conditional
            # UPDATE so we don't rebirth a terminal run.
            with self._session_factory() as session:
                result = session.execute(
                    update(RunORM)
                    .where(RunORM.id == run_id)
                    .where(RunORM.status.in_(self._FAILED_SOURCE_STATES))
                    .values(status=RunStatus.FAILED, completed_at=now)
                )
                session.commit()
                if result.rowcount == 0:
                    log.warning(
                        "Run %s already terminal (or absent) at "
                        "_mark_failed; skipping terminal transition + "
                        "RUN_FAILED event",
                        run_id,
                    )
                    return
        self._recorder.record(
            run_id=run_id,
            event_type=ProvenanceEventType.RUN_FAILED,
            actor=self._actor,
            payload={"reason": reason, "failure_class": failure_class},
        )


def run_sync(executor: LocalExecutor, run_id: UUID) -> ExecutionResult:
    """Sync wrapper for callers outside an async context (e.g. CLI,
    tests that don't want to spin up an event loop manually)."""
    return asyncio.run(executor.execute(run_id))


__all__ = ["ExecutionResult", "LocalExecutor", "run_sync"]
