"""ProvenanceRecorder — hash-chained append-only provenance log (T09 AC3).

Each ``ProvenanceEvent`` row belongs to a run-scoped chain. The first event
in a run has ``prev_event_hash = NULL``; every subsequent event's
``prev_event_hash`` equals the previous event's ``event_hash``.

``event_hash`` is SHA-256 over a canonical serialization of:

    prev_event_hash || run_id || event_type.value || actor || timestamp_iso || canonical_json(payload)

Canonical JSON uses ``sort_keys=True`` and tight separators so that
functionally-equal payloads produce the same hash regardless of dict
iteration order.

Concurrency: a single ``ProvenanceRecorder`` instance serializes writes
within a process via a threading lock. For multi-process deployments, use
a DB-level advisory lock or a SERIALIZABLE transaction upstream; this
class is designed for the laptop single-process Control Plane.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
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
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


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

    def record(
        self,
        run_id: UUID,
        event_type: ProvenanceEventType,
        actor: str,
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ProvenanceEvent:
        ts = now or datetime.now(timezone.utc)
        with self._lock, self._session_factory() as session:
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
