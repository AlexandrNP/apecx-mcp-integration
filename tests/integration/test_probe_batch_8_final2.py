"""Probe batch 8 — final 2 probes to close out the streak.

Probes 201-202.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


# --- Probe 201: Recorder commit-failure leaves cache untouched ---


def test_probe_201_recorder_no_partial_cache_on_commit_failure(
    cp_engine: Engine,
) -> None:
    """If session.commit() raises (e.g., FK violation), the
    cluster X cache must NOT be updated. Subsequent successful
    record() must still pick up the previous valid prev hash."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)

    # Seed a real run + record one event so cache has a valid entry.
    real_run = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(real_run), "ts": datetime.now(UTC).isoformat()},
        )
    e1 = recorder.record(
        run_id=real_run,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={},
    )
    assert recorder._last_hash[real_run] == e1.event_hash

    # Try to record on a non-existent run — FK violation.
    fake_run = uuid4()
    try:
        recorder.record(
            run_id=fake_run,
            event_type=ProvenanceEventType.RUN_STARTED,
            actor="probe",
            payload={},
        )
    except Exception:
        pass  # expected
    # Cache for fake_run was NOT populated (commit failed before
    # cache update).
    assert fake_run not in recorder._last_hash
    # Real run's cache is intact.
    assert recorder._last_hash[real_run] == e1.event_hash
    # Subsequent record on real_run uses correct prev.
    e2 = recorder.record(
        run_id=real_run,
        event_type=ProvenanceEventType.STEP_COMPLETED,
        actor="probe",
        payload={"i": 2},
    )
    assert e2.prev_event_hash == e1.event_hash


# --- Probe 202: Validate succeeds after recorder cache reset ---


def test_probe_202_validate_after_recorder_reset(cp_engine: Engine) -> None:
    """A fresh recorder validates an existing chain just fine."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    rid = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(rid), "ts": datetime.now(UTC).isoformat()},
        )
    for _ in range(3):
        recorder.record(
            run_id=rid,
            event_type=ProvenanceEventType.STEP_COMPLETED,
            actor="probe",
            payload={},
        )
    # Drop the original recorder; build a new one and validate.
    fresh = ProvenanceRecorder(factory)
    fresh.validate(rid)
