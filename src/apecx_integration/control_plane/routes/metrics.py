"""Review-UX telemetry (TX3).

Aggregates approval-decision timings so the team can see whether
reviewers are actually reviewing or rubber-stamping. AP §7 risk #4
calls this the project's most subtle failure mode — without
instrumentation we are guessing.

## Source of truth

We do NOT add ``approval_started_at`` columns to the Approval model.
Every approval already has two hash-chained provenance events:

- ``APPROVAL_REQUESTED`` — written when ``POST /approvals/`` creates
  the row.
- ``APPROVAL_DECIDED`` — written when ``approve/reject/correct``
  transitions the row.

Those events' timestamps are the authoritative started/decided pair,
and the hash chain makes after-the-fact tampering detectable. The
metrics endpoint just reads and aggregates.

## Rubber-stamping threshold

``rubber_stamping_suspected`` is ``True`` when the window has more
than 5 decisions AND the median time-to-decide is under 5 seconds —
the AP §7 risk-mitigation threshold. Documented in README so it
shows up at retros.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from apecx_integration.control_plane.dependencies import get_session
from apecx_integration.control_plane.models.entities import (
    Approval as ApprovalORM,
)
from apecx_integration.control_plane.models.entities import (
    ProvenanceEvent as ProvenanceEventORM,
)
from apecx_integration.control_plane.schemas.api import ApprovalMetricsResponse
from apecx_integration.control_plane.schemas.enums import (
    ApprovalStatus,
    ProvenanceEventType,
)

router = APIRouter(prefix="/metrics", tags=["metrics"])

RUBBER_STAMP_MIN_COUNT = 5
RUBBER_STAMP_MAX_MEDIAN_SECONDS = 5.0
P95_MIN_SAMPLES = 20


def _parse_since(since: str) -> datetime:
    """Parse an ISO-8601 timestamp; normalize to aware UTC.

    Naive timestamps are assumed UTC (matches _canonical_timestamp in
    the ProvenanceRecorder so hash-chain and metrics agree).
    """
    try:
        ts = datetime.fromisoformat(since)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"invalid `since` (expected ISO-8601): {e}",
        ) from e
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile, 0 < p < 100."""
    s = sorted(values)
    k = max(1, math.ceil(p / 100.0 * len(s)))
    return s[k - 1]


@router.get("/approvals", response_model=ApprovalMetricsResponse)
def approval_metrics(
    session: Annotated[Session, Depends(get_session)],
    since: Annotated[
        str,
        Query(
            description=(
                "ISO-8601 window start; approvals decided at or after "
                "this time are included. Example: 2026-04-15T00:00:00Z"
            )
        ),
    ],
) -> ApprovalMetricsResponse:
    window_start = _parse_since(since)

    # Pair up APPROVAL_REQUESTED and APPROVAL_DECIDED events by
    # approval_id (lives in payload["approval_id"]). Use SQLAlchemy
    # Core here because grouping across a JSON field is awkward with
    # the ORM; we pull events and aggregate in Python. At the laptop
    # scale targeted by the Control Plane, pulling a week's worth of
    # approval events (≤ few hundred rows) is negligible.
    stmt = (
        select(ProvenanceEventORM)
        .where(ProvenanceEventORM.event_type.in_(
            (ProvenanceEventType.APPROVAL_REQUESTED,
             ProvenanceEventType.APPROVAL_DECIDED)
        ))
        .where(ProvenanceEventORM.timestamp >= window_start)
        .order_by(ProvenanceEventORM.timestamp)
    )
    events = session.execute(stmt).scalars().all()

    started_at: dict[str, datetime] = {}
    decided_at: dict[str, datetime] = {}
    for e in events:
        payload = e.payload or {}
        approval_id = payload.get("approval_id")
        if not approval_id:
            continue
        if e.event_type is ProvenanceEventType.APPROVAL_REQUESTED:
            # Keep the earliest (a re-POST would be a bug but tolerate).
            if approval_id not in started_at:
                started_at[approval_id] = e.timestamp
        else:  # APPROVAL_DECIDED
            decided_at[approval_id] = e.timestamp

    paired_ids = [aid for aid in decided_at if aid in started_at]

    durations = []
    for aid in paired_ids:
        s = started_at[aid]
        d = decided_at[aid]
        # Normalize tz: SQLite strips tzinfo on read-back — same
        # convention as the ProvenanceRecorder's _canonical_timestamp.
        if s.tzinfo is None:
            s = s.replace(tzinfo=UTC)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        delta = (d - s).total_seconds()
        # Filter negatives defensively; they'd indicate clock skew or
        # data corruption — either way, a meaningless data point.
        if delta >= 0:
            durations.append(delta)

    count = len(durations)

    # Look up final statuses for the paired approvals to compute
    # percent_auto_approved / percent_rejected.
    status_counts = {s: 0 for s in ApprovalStatus}
    if paired_ids:
        rows = session.execute(
            select(ApprovalORM.id, ApprovalORM.status).where(
                ApprovalORM.id.in_([__import_uuid(a) for a in paired_ids])
            )
        ).all()
        for _id, status_val in rows:
            status_counts[status_val] += 1

    total_with_status = sum(status_counts.values()) or 1
    percent_auto_approved = (
        100.0 * status_counts[ApprovalStatus.AUTO_APPROVED] / total_with_status
    )
    percent_rejected = (
        100.0 * status_counts[ApprovalStatus.REJECTED] / total_with_status
    )

    median = _median(durations)
    p95 = _percentile(durations, 95.0) if count >= P95_MIN_SAMPLES else None

    rubber_stamping = (
        count > RUBBER_STAMP_MIN_COUNT
        and median is not None
        and median < RUBBER_STAMP_MAX_MEDIAN_SECONDS
    )

    return ApprovalMetricsResponse(
        count=count,
        median_time_to_decide_seconds=median,
        p95_time_to_decide_seconds=p95,
        percent_auto_approved=percent_auto_approved if count else 0.0,
        percent_rejected=percent_rejected if count else 0.0,
        rubber_stamping_suspected=rubber_stamping,
        window_start_iso=window_start.isoformat(),
    )


def __import_uuid(s: str):
    """Small indirection so the main query can pass strings through
    the UUIDString type-decorator without importing uuid at the top.
    """
    from uuid import UUID

    return UUID(s)
