"""TX3 integration: GET /metrics/approvals against a real Control Plane.

Uses the ProvenanceRecorder directly to seed APPROVAL_REQUESTED /
APPROVAL_DECIDED events with synthetic timings, then calls the endpoint
and asserts the aggregate shape. No mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import (
    ApprovalStatus,
    ProvenanceEventType,
)
from sqlalchemy import text

pytestmark = pytest.mark.integration


def _seed_run(engine, *, user_id: str = "alex") -> UUID:
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, 'PENDING', :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "ts": datetime.now(UTC).isoformat()},
        )
    return run_id


def _seed_decided_approval(
    engine,
    recorder: ProvenanceRecorder,
    run_id: UUID,
    *,
    seconds_to_decide: float,
    final_status: ApprovalStatus,
    base_time: datetime | None = None,
) -> UUID:
    """Write a REQUESTED+DECIDED event pair with a chosen time gap,
    and create the Approval row in the chosen final status.

    Does NOT go through the /approvals/ endpoint — we want full control
    over timestamps for the synthetic timing test.
    """
    approval_id = uuid4()
    step_id = uuid4()
    started = base_time or datetime.now(UTC)
    decided = started + timedelta(seconds=seconds_to_decide)

    with engine.begin() as conn:
        # Step row is required because Approval FKs to it.
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, status, "
                "input_artifact_ids, output_artifact_ids) "
                "VALUES (:id, :rid, 's', 'LOCAL', 'PAUSED_FOR_APPROVAL', '[]', '[]')"
            ),
            {"id": str(step_id), "rid": str(run_id)},
        )
        conn.execute(
            text(
                "INSERT INTO approval (id, step_id, kind, status, policy) "
                "VALUES (:id, :sid, 'HARD', :st, '{}')"
            ),
            {
                "id": str(approval_id),
                "sid": str(step_id),
                "st": final_status.value.upper(),
            },
        )

    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.APPROVAL_REQUESTED,
        actor="control_plane",
        payload={"approval_id": str(approval_id)},
        now=started,
    )
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.APPROVAL_DECIDED,
        actor="alex",
        payload={"approval_id": str(approval_id), "status": final_status.value},
        now=decided,
    )
    return approval_id


def test_empty_window_returns_zero_counts(cp_client, cp_engine) -> None:
    since = datetime.now(UTC).isoformat()
    resp = cp_client.get("/metrics/approvals", params={"since": since})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["median_time_to_decide_seconds"] is None
    assert body["p95_time_to_decide_seconds"] is None
    assert body["percent_auto_approved"] == 0.0
    assert body["percent_rejected"] == 0.0
    assert body["rubber_stamping_suspected"] is False


def test_ac3_ten_synthetic_approvals_produce_correct_median(cp_client, cp_engine) -> None:
    """TX3 AC3: seed 10 approvals with known timing, assert the median
    matches. Durations chosen so median is unambiguous: times
    [10, 20, 30, 40, 50, 60, 70, 80, 90, 100] -> median = 55.
    """
    run_id = _seed_run(cp_engine)
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    base = datetime.now(UTC)
    for seconds in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
        _seed_decided_approval(
            cp_engine,
            recorder,
            run_id,
            seconds_to_decide=float(seconds),
            final_status=ApprovalStatus.APPROVED,
            base_time=base,
        )

    since = (base - timedelta(seconds=1)).isoformat()
    resp = cp_client.get("/metrics/approvals", params={"since": since})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 10
    # Even n=10 -> average of 5th and 6th sorted = (50+60)/2 = 55
    assert body["median_time_to_decide_seconds"] == pytest.approx(55.0)
    # p95 is null below 20 samples per the documented threshold
    assert body["p95_time_to_decide_seconds"] is None
    assert body["percent_auto_approved"] == 0.0
    assert body["percent_rejected"] == 0.0
    assert body["rubber_stamping_suspected"] is False  # median 55s is plenty


def test_rubber_stamping_triggers_when_fast_and_frequent(cp_client, cp_engine) -> None:
    """Rubber-stamping: 6 approvals each decided in ~1s -> median ~1s,
    count > 5, threshold triggers.
    """
    run_id = _seed_run(cp_engine)
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    base = datetime.now(UTC)
    for _ in range(6):
        _seed_decided_approval(
            cp_engine,
            recorder,
            run_id,
            seconds_to_decide=1.0,
            final_status=ApprovalStatus.APPROVED,
            base_time=base,
        )

    resp = cp_client.get(
        "/metrics/approvals",
        params={"since": (base - timedelta(seconds=1)).isoformat()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 6
    assert body["median_time_to_decide_seconds"] == pytest.approx(1.0)
    assert body["rubber_stamping_suspected"] is True


def test_rubber_stamping_does_not_trigger_below_min_count(cp_client, cp_engine) -> None:
    """5 fast approvals (== min_count, not > min_count) -> no trigger,
    per the AP §7 threshold "N>5 approvals".
    """
    run_id = _seed_run(cp_engine)
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    base = datetime.now(UTC)
    for _ in range(5):
        _seed_decided_approval(
            cp_engine,
            recorder,
            run_id,
            seconds_to_decide=1.0,
            final_status=ApprovalStatus.APPROVED,
            base_time=base,
        )

    resp = cp_client.get(
        "/metrics/approvals",
        params={"since": (base - timedelta(seconds=1)).isoformat()},
    )
    assert resp.status_code == 200
    assert resp.json()["rubber_stamping_suspected"] is False


def test_percent_rejected_and_auto_approved(cp_client, cp_engine) -> None:
    """Seed mix of final statuses and assert the share aggregates."""
    run_id = _seed_run(cp_engine)
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    base = datetime.now(UTC)
    for _ in range(2):
        _seed_decided_approval(
            cp_engine,
            recorder,
            run_id,
            seconds_to_decide=30.0,
            final_status=ApprovalStatus.APPROVED,
            base_time=base,
        )
    _seed_decided_approval(
        cp_engine,
        recorder,
        run_id,
        seconds_to_decide=30.0,
        final_status=ApprovalStatus.REJECTED,
        base_time=base,
    )
    _seed_decided_approval(
        cp_engine,
        recorder,
        run_id,
        seconds_to_decide=30.0,
        final_status=ApprovalStatus.AUTO_APPROVED,
        base_time=base,
    )

    resp = cp_client.get(
        "/metrics/approvals",
        params={"since": (base - timedelta(seconds=1)).isoformat()},
    )
    body = resp.json()
    assert body["count"] == 4
    assert body["percent_rejected"] == pytest.approx(25.0)
    assert body["percent_auto_approved"] == pytest.approx(25.0)


def test_invalid_since_returns_400(cp_client) -> None:
    resp = cp_client.get("/metrics/approvals", params={"since": "not-a-date"})
    assert resp.status_code == 400
    assert "invalid `since`" in resp.json()["detail"].lower()


def test_missing_since_returns_422(cp_client) -> None:
    """FastAPI's query-param validator rejects missing required params."""
    resp = cp_client.get("/metrics/approvals")
    assert resp.status_code == 422


def test_window_excludes_older_events(cp_client, cp_engine) -> None:
    """Two approvals, one before `since`, one after -> count==1."""
    run_id = _seed_run(cp_engine)
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    far_past = datetime.now(UTC) - timedelta(days=30)
    near_past = datetime.now(UTC) - timedelta(minutes=1)

    _seed_decided_approval(
        cp_engine,
        recorder,
        run_id,
        seconds_to_decide=10.0,
        final_status=ApprovalStatus.APPROVED,
        base_time=far_past,
    )
    _seed_decided_approval(
        cp_engine,
        recorder,
        run_id,
        seconds_to_decide=10.0,
        final_status=ApprovalStatus.APPROVED,
        base_time=near_past,
    )

    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    resp = cp_client.get("/metrics/approvals", params={"since": since})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
