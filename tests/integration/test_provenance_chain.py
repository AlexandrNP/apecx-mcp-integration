"""T09 AC3: ProvenanceRecorder writes a hash-chained append-only log.

Integration test against a real SQLite file with migrations applied. Hash
chain is computed at write time; validate() walks the chain and rejects
any event whose recomputed hash disagrees with the stored one OR whose
prev_event_hash does not match its predecessor's event_hash.

Per workspace CLAUDE.md: this is an integration test (real DB, no mocks).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import (
    ChainBroken,
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cp_engine(tmp_path: Path):
    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    # The Run must exist before we can FK-reference it.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) " "VALUES (:id, :uid, :st, :ts)"
            ),
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "uid": "tester",
                "st": "PENDING",
                "ts": datetime.now(UTC).isoformat(),
            },
        )
    return engine


@pytest.mark.integration
def test_first_event_has_null_prev_hash(cp_engine) -> None:
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    run_id = uuid4()
    # Seed a run row for this new id.
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'tester', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="system",
        payload={"note": "first"},
    )
    assert evt.prev_event_hash is None
    assert len(evt.event_hash) == 64


@pytest.mark.integration
def test_chain_links_second_to_first(cp_engine) -> None:
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'tester', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )
    e1 = recorder.record(run_id, ProvenanceEventType.RUN_STARTED, "system", {"i": 1})
    e2 = recorder.record(run_id, ProvenanceEventType.STEP_STARTED, "system", {"i": 2})
    e3 = recorder.record(run_id, ProvenanceEventType.STEP_COMPLETED, "system", {"i": 3})
    assert e2.prev_event_hash == e1.event_hash
    assert e3.prev_event_hash == e2.event_hash
    assert e1.event_hash != e2.event_hash != e3.event_hash


@pytest.mark.integration
def test_validate_passes_on_clean_chain(cp_engine) -> None:
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'tester', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )
    for i in range(5):
        recorder.record(run_id, ProvenanceEventType.STEP_STARTED, "system", {"i": i})
    # validate() returns None on clean chain; raises on broken.
    recorder.validate(run_id)


@pytest.mark.integration
def test_validate_detects_tampered_payload(cp_engine) -> None:
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'tester', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )
    recorder.record(run_id, ProvenanceEventType.RUN_STARTED, "system", {"step": 1})
    recorder.record(run_id, ProvenanceEventType.STEP_COMPLETED, "system", {"step": 2})

    # Tamper: mutate the first event's payload directly in the DB.
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE provenance_event SET payload = :p "
                "WHERE run_id = :rid AND event_type = 'RUN_STARTED'"
            ),
            {"p": '{"step": 99}', "rid": str(run_id)},
        )

    with pytest.raises(ChainBroken) as excinfo:
        recorder.validate(run_id)
    assert "hash mismatch" in str(excinfo.value).lower()


@pytest.mark.integration
def test_validate_detects_tampered_prev_hash(cp_engine) -> None:
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'tester', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )
    e1 = recorder.record(run_id, ProvenanceEventType.RUN_STARTED, "system", {})
    recorder.record(run_id, ProvenanceEventType.STEP_STARTED, "system", {})

    # Tamper: point e2's prev_event_hash at a fake value.
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE provenance_event SET prev_event_hash = :ph "
                "WHERE run_id = :rid AND id != :e1id"
            ),
            {"ph": "0" * 64, "rid": str(run_id), "e1id": str(e1.id)},
        )

    with pytest.raises(ChainBroken):
        recorder.validate(run_id)


@pytest.mark.integration
def test_chain_is_per_run(cp_engine) -> None:
    """Two parallel runs each get their own chain; they don't interleave."""
    recorder = ProvenanceRecorder(make_session_factory(cp_engine))
    run_a = uuid4()
    run_b = uuid4()
    with cp_engine.begin() as conn:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'tester', 'PENDING', :ts)"
            ),
            [
                {"id": str(run_a), "ts": now},
                {"id": str(run_b), "ts": now},
            ],
        )
    a1 = recorder.record(run_a, ProvenanceEventType.RUN_STARTED, "system", {"r": "a"})
    b1 = recorder.record(run_b, ProvenanceEventType.RUN_STARTED, "system", {"r": "b"})
    a2 = recorder.record(run_a, ProvenanceEventType.STEP_STARTED, "system", {"r": "a2"})
    assert a1.prev_event_hash is None
    assert b1.prev_event_hash is None
    assert a2.prev_event_hash == a1.event_hash  # not b1
    recorder.validate(run_a)
    recorder.validate(run_b)
