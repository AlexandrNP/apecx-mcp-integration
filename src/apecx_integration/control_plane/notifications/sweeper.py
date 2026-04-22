"""Stale-run sweeper (T08 AP §5.8 "queue sweeper").

Detects runs in an active state (``RUNNING`` or ``PAUSED``) whose
most recent provenance event is older than a configurable threshold.
These are either:

- **Abandoned** — the executor died without emitting a terminal event
  (process crash, OOM-kill, lost heartbeat). Reconciliation: flip
  the run to ``FAILED`` with a provenance note explaining why.
- **Legitimately slow** — a long-running step that just hasn't
  emitted events. Reconciliation: do nothing; the sweeper's
  threshold should be comfortably above the slowest expected step.

The plan's AP §5.8 scope also calls for HPC-queue reconciliation
(poll the scheduler for abandoned runs). That branch is only
relevant when ``executor_kind == GLOBUS_COMPUTE`` or ``PBS_BUNDLE``;
for the local-default deployment, the "stale latest event"
heuristic is both necessary and sufficient. HPC reconciliation is
logged in docs/future_work.md as part of T04 / T05 / T07 follow-ups.

## Surface

``RunStateSweeper(session_factory, recorder).sweep(stale_after=...)``
returns a list of ``SweepResult`` describing each run that was
flagged. Caller is expected to log / notify on the results. The
sweeper itself does not call the email notifier — keeping
notification policy one layer up lets tests exercise the two
concerns independently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from apecx_integration.control_plane.models.entities import (
    ProvenanceEvent as ProvenanceEventORM,
)
from apecx_integration.control_plane.models.entities import Run as RunORM
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import (
    ProvenanceEventType,
    RunStatus,
)

log = logging.getLogger(__name__)

SWEEPABLE_STATES: frozenset[RunStatus] = frozenset({RunStatus.RUNNING, RunStatus.PAUSED})

DEFAULT_STALE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True, kw_only=True)
class SweepResult:
    run_id: UUID
    user_id: str
    old_status: RunStatus
    new_status: RunStatus
    last_event_at: datetime | None
    reason: str


class RunStateSweeper:
    """Sweeps for stale runs and reconciles their state.

    Not a background thread. Caller decides when to run ``sweep()``
    (cron, scheduler, or inline from a health-probe endpoint). This
    keeps the sweeper testable without asyncio-fixture gymnastics.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        recorder: ProvenanceRecorder,
    ) -> None:
        self._session_factory = session_factory
        self._recorder = recorder

    def sweep(
        self,
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        now: datetime | None = None,
    ) -> list[SweepResult]:
        """Find and reconcile stale runs.

        A run is stale iff:
          - its status is in ``SWEEPABLE_STATES``, AND
          - the latest provenance event for its run_id is older than
            ``stale_after``, OR it has no provenance events at all and
            its ``created_at`` is older than ``stale_after``.

        For each stale run, this method:
          - flips its status to ``FAILED`` (local-mode default;
            non-local executor reconciliation is future work);
          - records an ``RUN_FAILED`` provenance event explaining the
            sweep decision;
          - returns a :class:`SweepResult` describing the transition.
        """
        reference = now or datetime.now(UTC)
        cutoff = reference - stale_after
        results: list[SweepResult] = []

        with self._session_factory() as session:
            candidate_runs = (
                session.execute(select(RunORM).where(RunORM.status.in_(SWEEPABLE_STATES)))
                .scalars()
                .all()
            )

            for run in candidate_runs:
                last_event_at = session.execute(
                    select(func.max(ProvenanceEventORM.timestamp)).where(
                        ProvenanceEventORM.run_id == run.id
                    )
                ).scalar()

                # Normalize to aware UTC (SQLite strips tzinfo on
                # DateTime(timezone=True) round-trip; same convention
                # as the ProvenanceRecorder's _canonical_timestamp).
                if last_event_at is not None and last_event_at.tzinfo is None:
                    last_event_at = last_event_at.replace(tzinfo=UTC)
                run_created_at = run.created_at
                if run_created_at is not None and run_created_at.tzinfo is None:
                    run_created_at = run_created_at.replace(tzinfo=UTC)

                most_recent = last_event_at or run_created_at
                if most_recent is None or most_recent >= cutoff:
                    continue

                reason = (
                    f"last provenance event at {most_recent.isoformat()} "
                    f"is older than {stale_after} before reference time "
                    f"{reference.isoformat()}; sweeping to FAILED"
                )

                old_status = run.status
                run.status = RunStatus.FAILED
                run.completed_at = reference
                session.commit()
                session.refresh(run)

                self._recorder.record(
                    run_id=run.id,
                    event_type=ProvenanceEventType.RUN_FAILED,
                    actor="run_state_sweeper",
                    payload={
                        "sweep_reason": reason,
                        "previous_status": old_status.value,
                        "stale_after_seconds": int(stale_after.total_seconds()),
                    },
                    now=reference,
                )

                results.append(
                    SweepResult(
                        run_id=run.id,
                        user_id=run.user_id,
                        old_status=old_status,
                        new_status=RunStatus.FAILED,
                        last_event_at=most_recent,
                        reason=reason,
                    )
                )

        log.info("RunStateSweeper: swept %d stale runs", len(results))
        return results
