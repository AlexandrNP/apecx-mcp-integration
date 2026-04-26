"""ProvenanceRecorder — hash-chained append-only provenance log (T09 AC3).

Each ``ProvenanceEvent`` row belongs to a run-scoped chain. The first event
in a run has ``prev_event_hash = NULL``; every subsequent event's
``prev_event_hash`` equals the previous event's ``event_hash``.

``event_hash`` is SHA-256 over a canonical serialization of:

    prev_event_hash || run_id || event_type.value || actor || timestamp_iso || canonical_json(payload)

Canonical JSON uses ``sort_keys=True`` and tight separators so that
functionally-equal payloads produce the same hash regardless of dict
iteration order.

Concurrency model:
- Target: single process, multiple OS threads (FastAPI's sync-handler
  threadpool). ``threading.Lock`` is the correct primitive for that
  lane: it serializes write attempts between threads so two concurrent
  ``record`` calls cannot both read the same ``prev_event_hash`` and
  then write siblings that claim the same predecessor.
- If a caller moves this into a native-async handler (``async def``
  with an ``await`` reachable from under the lock), the blocking
  acquire pins the event loop. An async-aware primitive (e.g.,
  ``anyio.Lock``) would be needed then. The recorder has no awaits
  today, so this is only a live concern the moment an async code
  path wraps it.
- Multi-process deployment (gunicorn workers, multiple uvicorn
  processes) is out of scope: two processes can both read the same
  tail event and race. A DB advisory lock (Postgres) or SERIALIZABLE
  transaction would be required to extend to that lane.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apecx_integration.control_plane.models.entities import ProvenanceEvent
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType


class ChainBroken(Exception):
    """Raised when ProvenanceRecorder.validate finds a chain violation."""


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _canonical_timestamp(ts: datetime) -> str:
    """Render a timestamp in a form that survives SQLite round-trip.

    SQLite has no native timezone type; ``DateTime(timezone=True)`` columns
    serialize to ISO strings on write but the tzinfo is lost on read-back
    in some drivers. We assume every timestamp we write is UTC (the
    recorder always calls ``datetime.now(timezone.utc)``), so on read we
    reattach UTC if it is missing, then emit a canonical UTC isoformat.
    Without this, the write-time hash (aware) and the validate-time hash
    (naive) disagree and every chain looks broken.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat()


def _compute_event_hash(
    *,
    prev_event_hash: str | None,
    run_id: UUID,
    event_type: ProvenanceEventType,
    actor: str,
    timestamp: datetime,
    payload: dict[str, Any],
) -> str:
    h = hashlib.sha256()
    h.update((prev_event_hash or "").encode("utf-8"))
    h.update(b"\n")
    h.update(str(run_id).encode("utf-8"))
    h.update(b"\n")
    h.update(event_type.value.encode("utf-8"))
    h.update(b"\n")
    h.update(actor.encode("utf-8"))
    h.update(b"\n")
    h.update(_canonical_timestamp(timestamp).encode("utf-8"))
    h.update(b"\n")
    h.update(_canonical_json(payload).encode("utf-8"))
    return h.hexdigest()


class ProvenanceRecorder:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._lock = threading.Lock()
        # Per-run in-memory hash cursor. Updated inside the lock
        # after each successful commit. Read inside the lock before
        # computing the next event's prev_event_hash.
        #
        # Why a cache instead of a DB query: the DB-side
        # ``_last_event_for_run`` orders by ``(timestamp DESC, id
        # DESC)``. When N concurrent record() calls share the same
        # microsecond timestamp (cheap on fast hardware), the
        # ``id DESC`` tiebreaker is on a UUID — random, not
        # monotonic — so the SELECT can return an OLDER event as
        # "latest" even when a newer one is committed. This caused
        # the chain to fork: multiple writers all picked the same
        # predecessor (the lex-largest UUID with the tied
        # timestamp), producing N events with identical
        # ``prev_event_hash``. Found 2026-04-26 by adversarial test
        # cluster R follow-up; reproduced 9/10 trials at K=20
        # concurrent appends on a shared run.
        #
        # The fix: track the last-written hash per run in a
        # process-local dict, guarded by the same threading.Lock
        # that already serializes the write path. This is a single-
        # process invariant; cross-process operation would need a
        # SQL-level lease (audit follow-up if the Control Plane ever
        # multi-process). On startup, the cache is empty — the
        # fallback DB query handles the first record() per run.
        self._last_hash: dict[UUID, str] = {}

    def record(
        self,
        run_id: UUID,
        event_type: ProvenanceEventType,
        actor: str,
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ProvenanceEvent:
        ts = now or datetime.now(UTC)
        with self._lock, self._session_factory() as session:
            # Prefer the in-memory cursor (set under this same lock
            # by every prior committed call); fall back to the DB on
            # cold start. The DB fallback's UUID-tiebreak ambiguity
            # is harmless on cold start because there's at most one
            # event per run.
            prev_hash = self._last_hash.get(run_id)
            if prev_hash is None:
                prev = self._last_event_for_run(session, run_id)
                prev_hash = prev.event_hash if prev else None
            event_hash = _compute_event_hash(
                prev_event_hash=prev_hash,
                run_id=run_id,
                event_type=event_type,
                actor=actor,
                timestamp=ts,
                payload=payload,
            )
            evt = ProvenanceEvent(
                run_id=run_id,
                event_type=event_type,
                actor=actor,
                timestamp=ts,
                payload=payload,
                prev_event_hash=prev_hash,
                event_hash=event_hash,
            )
            session.add(evt)
            session.commit()
            session.refresh(evt)
            # Update cursor only AFTER the commit succeeds. If the
            # commit raises (e.g., the unique RUN_STARTED index from
            # migration 0002), the cursor stays at the pre-call
            # value — correct behavior.
            self._last_hash[run_id] = event_hash
            return evt

    def validate(self, run_id: UUID) -> None:
        """Walk the chain for ``run_id`` in timestamp order, raising
        :class:`ChainBroken` on the first violation.
        """
        with self._session_factory() as session:
            events = list(
                session.execute(
                    select(ProvenanceEvent)
                    .where(ProvenanceEvent.run_id == run_id)
                    .order_by(ProvenanceEvent.timestamp, ProvenanceEvent.id)
                ).scalars()
            )
        expected_prev: str | None = None
        for idx, e in enumerate(events):
            if e.prev_event_hash != expected_prev:
                raise ChainBroken(
                    f"event {idx} (id={e.id}) prev_event_hash link mismatch: "
                    f"expected {expected_prev!r}, got {e.prev_event_hash!r}"
                )
            recomputed = _compute_event_hash(
                prev_event_hash=e.prev_event_hash,
                run_id=e.run_id,
                event_type=e.event_type,
                actor=e.actor,
                timestamp=e.timestamp,
                payload=e.payload,
            )
            if recomputed != e.event_hash:
                raise ChainBroken(
                    f"event {idx} (id={e.id}) hash mismatch: "
                    f"stored={e.event_hash}, recomputed={recomputed}"
                )
            expected_prev = e.event_hash

    @staticmethod
    def _last_event_for_run(session: Session, run_id: UUID) -> ProvenanceEvent | None:
        return session.execute(
            select(ProvenanceEvent)
            .where(ProvenanceEvent.run_id == run_id)
            .order_by(ProvenanceEvent.timestamp.desc(), ProvenanceEvent.id.desc())
            .limit(1)
        ).scalar_one_or_none()
