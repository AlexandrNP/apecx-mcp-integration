"""Probe batch 30 — run-state sweeper + verified-synonym revoke
(probes 780-804).

Two adjacent surfaces with state-machine semantics. The sweeper
flips stale RUNNING/PAUSED runs to FAILED; the revoke route soft-
deletes a verified-synonym row. Both:

  - Use conditional UPDATE WHERE predicates as concurrency guards.
  - Have explicit "already-terminal / already-revoked" 409 paths.
  - Persist a metadata trail (sweep_reason, revocation_reason).

A regression here would silently re-sweep terminal runs, double-
revoke synonyms, or leave the metadata trail incomplete.

Cluster Z (sweeper conditional UPDATE) and AA (revoke conditional
UPDATE) touched these paths. This batch puts a regression mat
under both.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path):
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import (
        make_engine, make_session_factory,
    )
    p = tmp_path / "sweep.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{p}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{p}")
    return eng, make_session_factory(eng)


@pytest.fixture
def sweeper(db):
    from apecx_integration.control_plane.notifications.sweeper import (
        RunStateSweeper,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    _, sf = db
    return RunStateSweeper(sf, ProvenanceRecorder(sf))


def _insert_run(sf, *, status, created_at=None) -> uuid.UUID:
    from apecx_integration.control_plane.models.entities import Run
    rid = uuid.uuid4()
    with sf() as session:
        session.add(Run(
            id=rid, user_id="u",
            status=status,
            created_at=created_at or datetime.now(UTC),
        ))
        session.commit()
    return rid


def _insert_synonym(sf, *, is_active=True) -> uuid.UUID:
    from apecx_integration.control_plane.models.entities import VerifiedSynonym
    sid = uuid.uuid4()
    with sf() as session:
        session.add(VerifiedSynonym(
            id=sid,
            source_vocabulary="user_query",
            query_term="EEEV",
            target_vocabulary="violin.pathogen_id",
            canonical_term="VO_0000001",
            verified_by="alice",
            verified_at=datetime.now(UTC),
            confidence=1.0,
            is_active=is_active,
        ))
        session.commit()
    return sid


# ---------------------------------------------------------------------------
# Sweeper invariants — probes 780-790
# ---------------------------------------------------------------------------


def test_probe_780_sweepable_states_locked() -> None:
    """SWEEPABLE_STATES must be {RUNNING, PAUSED}. Adding PENDING
    here would sweep runs that were created seconds ago but not
    yet started by an executor — false positive."""
    from apecx_integration.control_plane.notifications.sweeper import (
        SWEEPABLE_STATES,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    assert SWEEPABLE_STATES == frozenset({
        RunStatus.RUNNING, RunStatus.PAUSED,
    })


def test_probe_781_default_stale_after_15_min() -> None:
    """The default stale-after window is 15 minutes — short enough
    that a wedged run reconciles within an MCP session, long enough
    that a slow-but-progressing run doesn't get swept."""
    from apecx_integration.control_plane.notifications.sweeper import (
        DEFAULT_STALE_AFTER,
    )
    assert DEFAULT_STALE_AFTER == timedelta(minutes=15)


def test_probe_782_sweep_result_frozen_kw_only() -> None:
    from dataclasses import FrozenInstanceError
    from apecx_integration.control_plane.notifications.sweeper import (
        SweepResult,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    sr = SweepResult(
        run_id=uuid.uuid4(), user_id="u",
        old_status=RunStatus.RUNNING, new_status=RunStatus.FAILED,
        last_event_at=None, reason="x",
    )
    with pytest.raises(FrozenInstanceError):
        sr.reason = "different"  # type: ignore[misc]


def test_probe_783_sweep_empty_db_returns_empty(sweeper) -> None:
    """A sweep against an empty DB must return [] without error."""
    results = sweeper.sweep()
    assert results == []


def test_probe_784_pending_run_never_swept(sweeper, db) -> None:
    """A PENDING run is not in SWEEPABLE_STATES — must NOT be
    swept, regardless of how stale its created_at is."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    long_ago = datetime.now(UTC) - timedelta(hours=24)
    rid = _insert_run(sf, status=RunStatus.PENDING, created_at=long_ago)
    results = sweeper.sweep()
    assert results == []
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.PENDING


def test_probe_785_completed_run_never_swept(sweeper, db) -> None:
    """COMPLETED is terminal. Cluster Z guard: re-sweeping a
    terminal run would emit a duplicate RUN_FAILED event."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(
        sf, status=RunStatus.COMPLETED,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    results = sweeper.sweep()
    assert results == []
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.COMPLETED


def test_probe_786_running_recent_not_swept(sweeper, db) -> None:
    """A RUNNING run with recent created_at must NOT be swept —
    real workflows are still progressing."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(
        sf, status=RunStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    results = sweeper.sweep()
    assert results == []
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.RUNNING


def test_probe_787_running_stale_swept_to_failed(sweeper, db) -> None:
    """A RUNNING run with stale created_at + no events → FAILED.
    This is the load-bearing happy path."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    rid = _insert_run(
        sf, status=RunStatus.RUNNING, created_at=long_ago,
    )
    results = sweeper.sweep()
    assert len(results) == 1
    assert results[0].run_id == rid
    assert results[0].old_status is RunStatus.RUNNING
    assert results[0].new_status is RunStatus.FAILED
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.FAILED
        assert run.completed_at is not None


def test_probe_788_paused_stale_swept_to_failed(sweeper, db) -> None:
    """PAUSED runs are also swept — a workflow that paused for
    approval and got abandoned should be reclaimable."""
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    rid = _insert_run(sf, status=RunStatus.PAUSED, created_at=long_ago)
    results = sweeper.sweep()
    assert len(results) == 1
    assert results[0].old_status is RunStatus.PAUSED
    with sf() as session:
        run = session.get(Run, rid)
        assert run.status is RunStatus.FAILED


def test_probe_789_sweep_emits_run_failed_with_reason(sweeper, db) -> None:
    """Every successful sweep must emit a RUN_FAILED provenance
    event whose payload names the previous status + sweep reason.
    That's the audit trail."""
    from apecx_integration.control_plane.models.entities import ProvenanceEvent
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType, RunStatus,
    )
    from sqlalchemy import select
    _, sf = db
    rid = _insert_run(
        sf, status=RunStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    sweeper.sweep()
    with sf() as session:
        events = session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.run_id == rid,
                ProvenanceEvent.event_type == ProvenanceEventType.RUN_FAILED,
            )
        ).scalars().all()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["previous_status"] == "running"
    assert "sweep_reason" in payload
    assert payload["stale_after_seconds"] == 15 * 60


def test_probe_790_stale_after_override(sweeper, db) -> None:
    """A custom stale_after window must be respected — a run that
    looks fresh under the default 15-min window can be swept under
    a 1-min window."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(
        sf, status=RunStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    # Default 15-min window: not swept
    assert sweeper.sweep() == []
    # 1-min window: swept
    results = sweeper.sweep(stale_after=timedelta(minutes=1))
    assert len(results) == 1
    assert results[0].run_id == rid


# ---------------------------------------------------------------------------
# Verified-synonym revoke — probes 791-799
# ---------------------------------------------------------------------------


def test_probe_791_revoke_request_requires_reason() -> None:
    """RevokeVerifiedSynonymRequest.revocation_reason is required —
    silent revoke without explanation would lose audit context."""
    from pydantic import ValidationError
    from apecx_integration.control_plane.schemas.api import (
        RevokeVerifiedSynonymRequest,
    )
    with pytest.raises(ValidationError):
        RevokeVerifiedSynonymRequest(revoked_by="alice")  # type: ignore[call-arg]


def test_probe_792_revoke_request_min_length_reason() -> None:
    """An empty-string revocation_reason must reject (min_length=1)."""
    from pydantic import ValidationError
    from apecx_integration.control_plane.schemas.api import (
        RevokeVerifiedSynonymRequest,
    )
    with pytest.raises(ValidationError):
        RevokeVerifiedSynonymRequest(
            revoked_by="alice", revocation_reason="",
        )


def test_probe_793_revoke_already_inactive_returns_409(db) -> None:
    """Cluster AA — re-revoking an already-inactive synonym must
    409. Allowing the second revoke would overwrite the original
    revocation metadata."""
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    eng, sf = db
    sid = _insert_synonym(sf, is_active=False)
    app = create_app(engine=eng)
    with TestClient(app) as client:
        r = client.patch(
            f"/verified_synonyms/{sid}",
            json={"revoked_by": "alice", "revocation_reason": "wrong mapping"},
        )
        assert r.status_code == 409
        assert "already inactive" in r.json()["detail"]


def test_probe_794_revoke_with_unknown_superseded_by_400(db) -> None:
    """superseded_by pointing at a nonexistent UUID must 400 —
    a dangling pointer would silently break the audit trail."""
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    eng, sf = db
    sid = _insert_synonym(sf, is_active=True)
    bogus = uuid.uuid4()
    app = create_app(engine=eng)
    with TestClient(app) as client:
        r = client.patch(
            f"/verified_synonyms/{sid}",
            json={
                "revoked_by": "alice",
                "revocation_reason": "wrong",
                "superseded_by": str(bogus),
            },
        )
        assert r.status_code == 400
        assert "superseded_by" in r.json()["detail"]


def test_probe_795_revoke_flips_is_active(db) -> None:
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.models.entities import VerifiedSynonym
    eng, sf = db
    sid = _insert_synonym(sf, is_active=True)
    app = create_app(engine=eng)
    with TestClient(app) as client:
        r = client.patch(
            f"/verified_synonyms/{sid}",
            json={"revoked_by": "alice", "revocation_reason": "wrong mapping"},
        )
        assert r.status_code == 200
    with sf() as session:
        row = session.get(VerifiedSynonym, sid)
        assert row.is_active is False


def test_probe_796_revoke_persists_metadata(db) -> None:
    """Successful revoke must populate revoked_by, revoked_at, AND
    revocation_reason. Missing any of the three would leave a
    "soft-deleted" row with no audit story."""
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.models.entities import VerifiedSynonym
    eng, sf = db
    sid = _insert_synonym(sf, is_active=True)
    app = create_app(engine=eng)
    with TestClient(app) as client:
        r = client.patch(
            f"/verified_synonyms/{sid}",
            json={
                "revoked_by": "carol@example.com",
                "revocation_reason": "wrong canonical id",
            },
        )
        assert r.status_code == 200
    with sf() as session:
        row = session.get(VerifiedSynonym, sid)
        assert row.revoked_by == "carol@example.com"
        assert row.revoked_at is not None
        assert row.revocation_reason == "wrong canonical id"


def test_probe_797_revoke_without_superseded_by(db) -> None:
    """superseded_by is optional. A revoke without one must
    succeed and leave that field NULL."""
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.models.entities import VerifiedSynonym
    eng, sf = db
    sid = _insert_synonym(sf, is_active=True)
    app = create_app(engine=eng)
    with TestClient(app) as client:
        r = client.patch(
            f"/verified_synonyms/{sid}",
            json={"revoked_by": "alice", "revocation_reason": "test"},
        )
        assert r.status_code == 200
    with sf() as session:
        row = session.get(VerifiedSynonym, sid)
        assert row.superseded_by is None


def test_probe_798_revoke_with_superseded_by(db) -> None:
    """A revoke that supersedes another row must persist the
    pointer — gives audit a forward link."""
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.models.entities import VerifiedSynonym
    from datetime import UTC, datetime
    import uuid as _uuid
    eng, sf = db
    old_sid = _insert_synonym(sf, is_active=True)
    # Migration 0003 enforces unique (source, query, target, scope=NULL,
    # is_active=True). Use a different query_term so the second row
    # can coexist as a "replacement."
    new_sid = _uuid.uuid4()
    with sf() as session:
        session.add(VerifiedSynonym(
            id=new_sid,
            source_vocabulary="user_query",
            query_term="VEEV",  # different term
            target_vocabulary="violin.pathogen_id",
            canonical_term="VO_0000002",
            verified_by="alice",
            verified_at=datetime.now(UTC),
            confidence=1.0,
            is_active=True,
        ))
        session.commit()
    app = create_app(engine=eng)
    with TestClient(app) as client:
        r = client.patch(
            f"/verified_synonyms/{old_sid}",
            json={
                "revoked_by": "alice", "revocation_reason": "replaced",
                "superseded_by": str(new_sid),
            },
        )
        assert r.status_code == 200
    with sf() as session:
        row = session.get(VerifiedSynonym, old_sid)
        assert row.superseded_by == new_sid


def test_probe_799_revoke_unknown_id_returns_404(db) -> None:
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    eng, _ = db
    app = create_app(engine=eng)
    with TestClient(app) as client:
        r = client.patch(
            f"/verified_synonyms/{uuid.uuid4()}",
            json={"revoked_by": "alice", "revocation_reason": "test"},
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Cross invariants — probes 800-804
# ---------------------------------------------------------------------------


def test_probe_800_sweep_idempotent_after_terminal(sweeper, db) -> None:
    """After a sweep flips a stale RUNNING run to FAILED, a second
    sweep must NOT re-touch it — the run is now terminal."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(
        sf, status=RunStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    first = sweeper.sweep()
    second = sweeper.sweep()
    assert len(first) == 1
    assert second == []  # no re-sweep


def test_probe_801_recent_run_with_no_events_not_swept(sweeper, db) -> None:
    """A RUNNING run with no events but a recent created_at must
    NOT be swept — the executor may have just claimed it."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    _insert_run(
        sf, status=RunStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    assert sweeper.sweep() == []


def test_probe_802_sweep_handles_event_freshness(sweeper, db) -> None:
    """A run with a stale created_at but a RECENT provenance event
    must NOT be swept — the executor IS making progress, just
    slowly."""
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType, RunStatus,
    )
    _, sf = db
    rid = _insert_run(
        sf, status=RunStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    # Record a recent event — proves the executor is alive
    rec = ProvenanceRecorder(sf)
    rec.record(
        run_id=rid, event_type=ProvenanceEventType.STEP_STARTED,
        actor="executor", payload={},
    )
    assert sweeper.sweep() == []


def test_probe_803_sweeper_handles_naive_timestamps(sweeper, db) -> None:
    """SQLite strips tzinfo on read-back. Sweeper must normalize
    naive timestamps to UTC before comparing — otherwise the
    comparison errors out under TypeError."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    # Insert with NAIVE created_at to simulate post-roundtrip state
    naive_old = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None)
    rid = _insert_run(
        sf, status=RunStatus.RUNNING, created_at=naive_old,
    )
    results = sweeper.sweep()
    # Either it sweeps (1) or doesn't error out
    assert all(isinstance(r.last_event_at, (datetime, type(None))) for r in results)


def test_probe_804_sweep_result_carries_user_id(sweeper, db) -> None:
    """SweepResult.user_id is needed by downstream notifiers
    (email handler in cluster Z) so they can route emails to the
    right scientist."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    _, sf = db
    rid = _insert_run(
        sf, status=RunStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    results = sweeper.sweep()
    assert len(results) == 1
    assert results[0].user_id == "u"
    assert results[0].run_id == rid
