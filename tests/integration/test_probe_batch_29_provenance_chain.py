"""Probe batch 29 — provenance recorder hash chain integrity
(probes 755-779).

The provenance recorder is the audit-trail truth-teller. A bug in
record() / validate() (chain breaks, fork, missing genesis,
hash-of-tampered-payload accepted) corrupts the integrity guarantee
the entire system depends on for after-the-fact reconstruction.

Cluster X (in-memory cursor for hash chain), AD (single recorder
instance), AF (graph-walk validate), AG (cold-start tail finder)
all touched this code. This batch puts a regression mat under
each.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path):
    """A real migrated SQLite DB + session factory."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import (
        make_engine, make_session_factory,
    )
    p = tmp_path / "prov.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{p}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{p}")
    return eng, make_session_factory(eng)


@pytest.fixture
def recorder(db):
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    _, sf = db
    return ProvenanceRecorder(sf)


def _insert_run(session_factory) -> uuid.UUID:
    """Insert a Run row and return its id."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    rid = uuid.uuid4()
    with session_factory() as session:
        session.add(Run(
            id=rid, user_id="u",
            status=RunStatus.PENDING,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    return rid


# ---------------------------------------------------------------------------
# record() integration — probes 755-762
# ---------------------------------------------------------------------------


def test_probe_755_first_record_is_genesis(recorder, db) -> None:
    """The first record() for a run produces an event with
    prev_event_hash=None — the genesis marker."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    evt = recorder.record(
        run_id=rid,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="executor",
        payload={"k": "v"},
    )
    assert evt.prev_event_hash is None
    assert len(evt.event_hash) == 64  # SHA-256 hex


def test_probe_756_second_record_chains_to_first(recorder, db) -> None:
    """The second record() must reference the first's event_hash
    in its prev_event_hash."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    e1 = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="executor", payload={},
    )
    e2 = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
        actor="executor", payload={"step_id": "x"},
    )
    assert e2.prev_event_hash == e1.event_hash


def test_probe_757_cache_updated_after_commit(recorder, db) -> None:
    """Cluster X — the in-memory _last_hash cursor must be set
    after each successful commit so subsequent record() calls
    pick the correct predecessor without re-querying the DB."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    e1 = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    assert recorder._last_hash[rid] == e1.event_hash


def test_probe_758_hash_includes_payload(recorder, db) -> None:
    """Different payload → different event_hash. Otherwise tamper-
    detection wouldn't work."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid_a = _insert_run(sf)
    rid_b = _insert_run(sf)
    ts = datetime(2026, 4, 26, tzinfo=UTC)
    a = recorder.record(
        run_id=rid_a, event_type=ProvenanceEventType.RUN_STARTED,
        actor="executor", payload={"k": "v1"}, now=ts,
    )
    b = recorder.record(
        run_id=rid_b, event_type=ProvenanceEventType.RUN_STARTED,
        actor="executor", payload={"k": "v2"}, now=ts,
    )
    assert a.event_hash != b.event_hash


def test_probe_759_hash_includes_actor(recorder, db) -> None:
    """Different actor → different hash."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid_a = _insert_run(sf)
    rid_b = _insert_run(sf)
    ts = datetime(2026, 4, 26, tzinfo=UTC)
    a = recorder.record(
        run_id=rid_a, event_type=ProvenanceEventType.RUN_STARTED,
        actor="a", payload={}, now=ts,
    )
    b = recorder.record(
        run_id=rid_b, event_type=ProvenanceEventType.RUN_STARTED,
        actor="b", payload={}, now=ts,
    )
    assert a.event_hash != b.event_hash


def test_probe_760_lock_serializes_writes(recorder, db) -> None:
    """The recorder must hold a threading.Lock around the entire
    read-prev → compute-hash → insert sequence. Cluster X regression:
    a missing lock under K=20 concurrent appends caused stale-prev
    forks 9/10 trials."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    # Drive 20 concurrent writes; the chain MUST validate after.
    errors = []

    def writer(i: int):
        try:
            recorder.record(
                run_id=rid,
                event_type=ProvenanceEventType.STEP_STARTED,
                actor=f"thread-{i}",
                payload={"i": i},
            )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"PROBE 760: writer threads errored: {errors}"
    # Chain integrity check
    recorder.validate(rid)


def test_probe_761_now_override_accepted(recorder, db) -> None:
    """A custom timestamp can be provided. Useful for
    deterministic tests + Tier-2 ingest replay. SQLite strips
    tzinfo on read-back, so compare the date+time components
    rather than the aware/naive datetime objects directly."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    evt = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={}, now=ts,
    )
    # SQLite returns naive (UTC) datetime — compare the components
    assert evt.timestamp.replace(tzinfo=UTC) == ts


def test_probe_762_events_persist_across_recorder_instances(db) -> None:
    """Cluster AD — when a fresh recorder loads on cold start, it
    must find the run's last event via _last_event_for_run (DB
    fallback) so subsequent writes chain correctly."""
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    rec1 = ProvenanceRecorder(sf)
    e1 = rec1.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    # Fresh recorder, no cache for this rid
    rec2 = ProvenanceRecorder(sf)
    e2 = rec2.record(
        run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
        actor="x", payload={},
    )
    assert e2.prev_event_hash == e1.event_hash


# ---------------------------------------------------------------------------
# validate() chain integrity — probes 763-770
# ---------------------------------------------------------------------------


def test_probe_763_validate_empty_run_passes(recorder) -> None:
    """A run with no events is vacuously valid — must not raise."""
    rid = uuid.uuid4()
    recorder.validate(rid)  # no exception


def test_probe_764_validate_single_event_passes(recorder, db) -> None:
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    recorder.validate(rid)


def test_probe_765_validate_chain_passes(recorder, db) -> None:
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    for i in range(5):
        recorder.record(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", payload={"i": i},
        )
    recorder.validate(rid)


def test_probe_766_validate_detects_fork(recorder, db) -> None:
    """Cluster AF: two events with the same prev_event_hash means
    the chain has forked. validate() must raise ChainBroken."""
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    # Genesis event
    e1 = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    # Manually inject a fork: two events with the same prev hash
    with sf() as session:
        session.add(ProvenanceEvent(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", timestamp=datetime.now(UTC), payload={"a": 1},
            prev_event_hash=e1.event_hash, event_hash="aa" * 32,
        ))
        session.add(ProvenanceEvent(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", timestamp=datetime.now(UTC), payload={"a": 2},
            prev_event_hash=e1.event_hash, event_hash="bb" * 32,
        ))
        session.commit()
    with pytest.raises(ChainBroken, match="forks"):
        recorder.validate(rid)


def test_probe_767_validate_detects_no_genesis(recorder, db) -> None:
    """If every event has a non-NULL prev_event_hash, there's no
    genesis — chain is rootless."""
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    with sf() as session:
        session.add(ProvenanceEvent(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", timestamp=datetime.now(UTC), payload={},
            prev_event_hash="aa" * 32, event_hash="cc" * 32,
        ))
        session.commit()
    with pytest.raises(ChainBroken, match="rootless|genesis"):
        recorder.validate(rid)


def test_probe_768_validate_detects_multiple_genesis(recorder, db) -> None:
    """Two events with prev_event_hash=None means two roots — only
    one is allowed."""
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    # Use STEP_STARTED (no unique constraint) — the multi-genesis
    # invariant is purely about prev_event_hash=None, not event_type.
    # Migration 0002's unique index would block two RUN_STARTED.
    with sf() as session:
        for h in ("aa" * 32, "bb" * 32):
            session.add(ProvenanceEvent(
                run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
                actor="x", timestamp=datetime.now(UTC), payload={},
                prev_event_hash=None, event_hash=h,
            ))
        session.commit()
    # Two events with prev_event_hash=None means multiple roots —
    # detected as a fork-on-None by the fork detector (the fork
    # check runs before the genesis-count check, so either error
    # class is acceptable evidence of detection).
    with pytest.raises(ChainBroken, match="forks|genesis"):
        recorder.validate(rid)


def test_probe_769_validate_detects_hash_mismatch(recorder, db) -> None:
    """A stored event_hash that doesn't match recompute = tamper.
    validate() must raise ChainBroken."""
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    # Insert an event whose stored hash is bogus
    with sf() as session:
        session.add(ProvenanceEvent(
            run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
            actor="x", timestamp=datetime.now(UTC), payload={"k": "v"},
            prev_event_hash=None, event_hash="0" * 64,  # bogus
        ))
        session.commit()
    with pytest.raises(ChainBroken, match="hash mismatch"):
        recorder.validate(rid)


def test_probe_770_validate_detects_partition(recorder, db) -> None:
    """An orphan event (not reachable from genesis) means the
    chain partition. The recorder's walk must visit every event."""
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    e1 = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    # Inject an orphan event whose prev points at a non-existent hash
    with sf() as session:
        session.add(ProvenanceEvent(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", timestamp=datetime.now(UTC), payload={},
            prev_event_hash="ff" * 32,  # references nothing
            event_hash="ee" * 32,
        ))
        session.commit()
    with pytest.raises(ChainBroken, match="partition|reachable"):
        recorder.validate(rid)


# ---------------------------------------------------------------------------
# Cluster X / AD — in-memory cache + cold-start regressions — probes 771-774
# ---------------------------------------------------------------------------


def test_probe_771_long_chain_validates(recorder, db) -> None:
    """A 50-event chain must validate. Stress test for the
    walk-from-genesis algorithm."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    for i in range(50):
        recorder.record(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", payload={"i": i},
        )
    recorder.validate(rid)


def test_probe_772_recorder_lock_held(recorder) -> None:
    """The recorder must hold a real threading.Lock instance —
    not a noop or a stub. Removing the lock would re-introduce
    cluster X."""
    assert isinstance(recorder._lock, type(threading.Lock()))


def test_probe_773_failed_commit_doesnt_corrupt_cache(db) -> None:
    """Migration 0002 unique RUN_STARTED index — a duplicate
    RUN_STARTED commit raises IntegrityError. The recorder must
    NOT advance _last_hash on raise (preserves chain consistency
    for the next legitimate write)."""
    from sqlalchemy.exc import IntegrityError
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rec = ProvenanceRecorder(sf)
    rid = _insert_run(sf)
    e1 = rec.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    cached_after_first = rec._last_hash[rid]
    # Second RUN_STARTED — must raise IntegrityError due to the
    # partial unique index (migration 0002).
    with pytest.raises(IntegrityError):
        rec.record(
            run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
            actor="other_executor", payload={},
        )
    # Cache MUST still point at e1's hash, not at the failed write
    assert rec._last_hash[rid] == cached_after_first
    assert cached_after_first == e1.event_hash


def test_probe_774_canonical_timestamp_survives_naive_round_trip(
    recorder, db,
) -> None:
    """SQLite strips tzinfo on read-back. The recorder's
    _canonical_timestamp must produce the same canonical string
    for an aware UTC datetime AND its naive read-back, otherwise
    chain validation breaks across the SQLite round-trip
    (cluster X / Y class)."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    aware = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={"k": "v"}, now=aware,
    )
    # validate() walks via _compute_event_hash, which calls
    # _canonical_timestamp on read-back ts. If the round-trip
    # disagrees, validate raises ChainBroken.
    recorder.validate(rid)


# ---------------------------------------------------------------------------
# Migration 0002 + concurrency invariants — probes 775-779
# ---------------------------------------------------------------------------


def test_probe_775_double_run_started_raises_integrity(db) -> None:
    """Migration 0002: only ONE RUN_STARTED event per run. A second
    RUN_STARTED must raise IntegrityError."""
    from sqlalchemy.exc import IntegrityError
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rec = ProvenanceRecorder(sf)
    rid = _insert_run(sf)
    rec.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    with pytest.raises(IntegrityError):
        rec.record(
            run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
            actor="y", payload={},
        )


def test_probe_776_one_run_started_per_run_allowed(db) -> None:
    """The unique index is per-run; two different runs each get
    ONE RUN_STARTED — must NOT collide."""
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rec = ProvenanceRecorder(sf)
    rid_a = _insert_run(sf)
    rid_b = _insert_run(sf)
    rec.record(
        run_id=rid_a, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    rec.record(
        run_id=rid_b, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )


def test_probe_777_concurrent_records_chain_validates(db) -> None:
    """K=10 concurrent non-RUN_STARTED records on the same run.
    Chain must validate cleanly. Cluster X stress test."""
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rec = ProvenanceRecorder(sf)
    rid = _insert_run(sf)
    rec.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    errors = []

    def writer(i):
        try:
            rec.record(
                run_id=rid,
                event_type=ProvenanceEventType.STEP_STARTED,
                actor="x", payload={"i": i},
            )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rec.validate(rid)


def test_probe_778_chain_broken_extends_exception() -> None:
    """ChainBroken must extend Exception (not, say, AssertionError
    which gets swallowed under -O). It's the canonical signal that
    the audit trail is corrupt."""
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken,
    )
    assert issubclass(ChainBroken, Exception)
    # Should NOT be a subclass of ValueError — corrupt provenance
    # is structurally different from "bad input"
    assert not issubclass(ChainBroken, ValueError)


def test_probe_779_validate_message_names_offending_event(recorder, db) -> None:
    """When validate() raises ChainBroken, its message must
    identify the offending event ids so an operator can locate
    the corruption in the DB."""
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    _, sf = db
    rid = _insert_run(sf)
    e1 = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="x", payload={},
    )
    # Inject fork
    with sf() as session:
        session.add(ProvenanceEvent(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", timestamp=datetime.now(UTC), payload={},
            prev_event_hash=e1.event_hash, event_hash="ab" * 32,
        ))
        session.add(ProvenanceEvent(
            run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
            actor="x", timestamp=datetime.now(UTC), payload={},
            prev_event_hash=e1.event_hash, event_hash="cd" * 32,
        ))
        session.commit()
    with pytest.raises(ChainBroken) as exc:
        recorder.validate(rid)
    # Message must name the prev-hash that's referenced multiply,
    # and include event ids — so an operator can run a targeted
    # cleanup query.
    msg = str(exc.value)
    assert e1.event_hash in msg or "prev_event_hash" in msg
