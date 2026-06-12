"""Session run store (EO-04): keep each workflow run's summary + envelope keyed by run_id.

``run_workflow`` (EO-03) generates a ``run_id`` per invocation and records the run here;
``inspect_run`` (EO-04) and ``apecx_context`` (EO-05) read it back. This is the
session-scoped, in-memory implementation — the deliberate default per the design's open
decision (``external_orchestration_design.md`` §11 / eo_implementation_log RESUME-HERE:
"in-memory session vs durable store"). A durable backend can replace the singleton later
without touching callers.

No silent failure: ``get`` returns ``None`` for an unknown ``run_id`` (the caller surfaces a
loud "unknown run_id" error); it never fabricates an empty record.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from apecx_integration.composition.runtime.provenance_wiring import RunSummary
from apecx_integration.composition.schemas.workflow_result import WorkflowResult


@dataclass
class RunRecord:
    """One recorded workflow run."""

    run_id: str
    workflow_name: str
    status: str | None
    run_summary: RunSummary
    workflow_result: WorkflowResult | None
    order: int  # monotonic per-store sequence — deterministic session ordering, no clock dep


class RunStore:
    """In-memory, thread-safe store of workflow runs keyed by ``run_id``."""

    def __init__(self) -> None:
        # RLock (not Lock): record() holds the lock and assigns run_id under it; keeping the
        # lock reentrant avoids the self-deadlock class that bit SynonymOverlay (2026-06-12).
        self._lock = threading.RLock()
        self._runs: dict[str, RunRecord] = {}
        self._counter = 0

    def record(
        self,
        *,
        workflow_name: str,
        status: str | None,
        run_summary: RunSummary,
        workflow_result: WorkflowResult | None,
    ) -> RunRecord:
        """Store a run under a fresh ``run_id`` and return the record."""
        with self._lock:
            self._counter += 1
            run_id = uuid.uuid4().hex
            record = RunRecord(
                run_id=run_id,
                workflow_name=workflow_name,
                status=status,
                run_summary=run_summary,
                workflow_result=workflow_result,
                order=self._counter,
            )
            self._runs[run_id] = record
            return record

    def get(self, run_id: str) -> RunRecord | None:
        """Return the record for ``run_id``, or ``None`` if it is unknown."""
        with self._lock:
            return self._runs.get(run_id)

    def session_runs(self) -> list[RunRecord]:
        """All runs recorded this session, oldest first."""
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.order)

    def clear(self) -> None:
        """Drop every recorded run (test hook + session reset)."""
        with self._lock:
            self._runs.clear()
            self._counter = 0


_singleton_lock = threading.Lock()
_singleton: RunStore | None = None


def get_run_store() -> RunStore:
    """Process-wide run store singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = RunStore()
        return _singleton
