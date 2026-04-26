"""Probe batch 6 — extra coverage of edge surfaces.

Probes 151-175.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def _seed_run(engine: Engine, *, status_value: str = "PENDING", user_id: str = "alex") -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, :st, :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "st": status_value, "ts": now},
        )
    return run_id


# --- Probe 151: /verified_synonyms create returns valid UUID id ---


def test_probe_151_create_returns_valid_uuid(cp_client: TestClient) -> None:
    r = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v",
            "query_term": "uuid-test",
            "target_vocabulary": "b",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.0,
            "scope": "uuid",
        },
    )
    UUID(r.json()["verified_synonym"]["id"])  # raises if invalid


# --- Probe 152: Multiple records to same run produce monotonically-extending chain ---


def test_probe_152_chain_extends_monotonically(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    e1 = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED, actor="p", payload={}
    )
    e2 = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.STEP_STARTED, actor="p", payload={}
    )
    e3 = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.STEP_COMPLETED, actor="p", payload={}
    )
    assert e2.prev_event_hash == e1.event_hash
    assert e3.prev_event_hash == e2.event_hash


# --- Probe 153: Recorder stores actor as-given (no transformation) ---


def test_probe_153_actor_round_trip(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    actor = "alex@example.com (special#chars)"
    evt = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED,
        actor=actor, payload={},
    )
    assert evt.actor == actor


# --- Probe 154: Validate works after a long chain (50 events) ---


def test_probe_154_validate_long_chain(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    for i in range(50):
        recorder.record(
            run_id=run_id, event_type=ProvenanceEventType.STEP_COMPLETED,
            actor="p", payload={"i": i},
        )
    recorder.validate(run_id)


# --- Probe 155: Cluster X cache invariant: cache hash matches DB ---


def test_probe_155_cache_matches_db(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    e = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED, actor="p", payload={}
    )
    with cp_engine.connect() as conn:
        db_hash = conn.execute(
            text("SELECT event_hash FROM provenance_event WHERE id = :id"),
            {"id": str(e.id)},
        ).scalar_one()
    assert recorder._last_hash[run_id] == db_hash


# --- Probe 156: Recorder rejects record() with empty actor ---


def test_probe_156_recorder_empty_actor(cp_engine: Engine) -> None:
    """Recorder accepts empty actor — schema doesn't enforce.
    Probe documents this; not a bug per the model."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    evt = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED,
        actor="", payload={},
    )
    assert evt.actor == ""


# --- Probe 157: Approval timestamps recorded on decide ---


def test_probe_157_approval_decided_at_set(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    run_id = _seed_run(cp_engine)
    step_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, "
                "status, input_artifact_ids, output_artifact_ids, "
                "created_at) VALUES (:id, :rid, 's', 'LOCAL', "
                "'PAUSED_FOR_APPROVAL', '[]', '[]', :ts)"
            ),
            {"id": str(step_id), "rid": str(run_id), "ts": now},
        )
    cr = cp_client.post(
        "/approvals/",
        json={
            "run_id": str(run_id), "step_id": str(step_id),
            "kind": "hard", "summary": "t", "artifact_ids": [],
        },
    )
    aid = cr.json()["approval"]["id"]
    cp_client.post(
        "/approvals/approve",
        json={"approval_id": aid, "decided_by": "alex"},
    )
    g = cp_client.get(f"/approvals/{aid}")
    body = g.json()["approval"]
    assert body["decided_by"] == "alex"
    assert body["decided_at"] is not None


# --- Probe 158: /verified_synonyms/lookup respects user-provided order ---


def test_probe_158_lookup_preserves_input_order(cp_client: TestClient) -> None:
    # Seed a few synonyms.
    for term in ["a", "b", "c"]:
        cp_client.post(
            "/verified_synonyms/",
            json={
                "source_vocabulary": "v",
                "query_term": term,
                "target_vocabulary": "b",
                "canonical_term": term.upper(),
                "verified_by": "alex",
                "confidence": 1.0,
                "scope": "ord",
            },
        )
    r = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "v",
            "target_vocabulary": "b",
            "query_terms": ["c", "a", "b"],
            "scope": "ord",
        },
    )
    matches = r.json()["matches"]
    assert [m["query_term"] for m in matches] == ["c", "a", "b"]


# --- Probe 159: /runs/list returns 0 for limit=1 if no runs ---


def test_probe_159_runs_list_no_runs(cp_client: TestClient) -> None:
    r = cp_client.post("/runs/list", json={"user_id": "nobody-here-ever"})
    assert r.status_code == 200
    assert r.json()["runs"] == []


# --- Probe 160: ConfirmAllocation rejects negative confirmed_core_hours ---


def test_probe_160_confirm_negative(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    run_id = _seed_run(cp_engine)
    r = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(run_id), "confirmed_core_hours": -1.0},
    )
    assert r.status_code == 422


# --- Probe 161: Sweeper with no runs returns empty list ---


def test_probe_161_sweeper_empty_db(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.notifications.sweeper import (
        RunStateSweeper,
    )

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    sweeper = RunStateSweeper(factory, recorder)
    results = sweeper.sweep()
    assert results == []


# --- Probe 162: Sweeper ignores PENDING runs even if old ---


def test_probe_162_sweeper_ignores_pending(cp_engine: Engine) -> None:
    from datetime import timedelta
    from apecx_integration.control_plane.notifications.sweeper import (
        RunStateSweeper,
    )

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    sweeper = RunStateSweeper(factory, recorder)
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(uuid4()), "ts": long_ago.isoformat()},
        )
    results = sweeper.sweep()
    assert results == []


# --- Probe 163: Sweeper ignores COMPLETED runs ---


def test_probe_163_sweeper_ignores_completed(cp_engine: Engine) -> None:
    from datetime import timedelta
    from apecx_integration.control_plane.notifications.sweeper import (
        RunStateSweeper,
    )

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    sweeper = RunStateSweeper(factory, recorder)
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'COMPLETED', :ts)"
            ),
            {"id": str(uuid4()), "ts": long_ago.isoformat()},
        )
    results = sweeper.sweep()
    assert results == []


# --- Probe 164: Sweeper ignores CANCELLED runs ---


def test_probe_164_sweeper_ignores_cancelled(cp_engine: Engine) -> None:
    from datetime import timedelta
    from apecx_integration.control_plane.notifications.sweeper import (
        RunStateSweeper,
    )

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    sweeper = RunStateSweeper(factory, recorder)
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'CANCELLED', :ts)"
            ),
            {"id": str(uuid4()), "ts": long_ago.isoformat()},
        )
    results = sweeper.sweep()
    assert results == []


# --- Probe 165: Recorder tail-finder picks the only event correctly ---


def test_probe_165_recorder_single_event_chain(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    e = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED, actor="p", payload={}
    )
    # _last_event_for_run with a fresh recorder (cluster AG)
    fresh = ProvenanceRecorder(factory)
    e2 = recorder._last_event_for_run.__func__(
        recorder, fresh._session_factory(), run_id
    ) if False else None
    # Easier: call via fresh recorder's record() — cold cache calls
    # _last_event_for_run internally.
    e3 = fresh.record(
        run_id=run_id, event_type=ProvenanceEventType.STEP_STARTED, actor="p", payload={}
    )
    assert e3.prev_event_hash == e.event_hash


# --- Probe 166: validate raises on malformed event_hash in DB ---


def test_probe_166_validate_detects_corrupt_hash(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.provenance.recorder import ChainBroken

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    e = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED, actor="p", payload={}
    )
    # Corrupt the stored hash externally.
    with cp_engine.begin() as conn:
        conn.execute(
            text("UPDATE provenance_event SET event_hash = :h WHERE id = :id"),
            {"h": "0" * 64, "id": str(e.id)},
        )
    with pytest.raises(ChainBroken):
        recorder.validate(run_id)


# --- Probe 167: /workflows/start with negative limit returns 422 (no limit field, but extra check) ---


def test_probe_167_runs_list_negative_limit(cp_client: TestClient) -> None:
    r = cp_client.post(
        "/runs/list",
        json={"user_id": "alex", "limit": -1},
    )
    assert r.status_code == 422


# --- Probe 168: /verified_synonyms create with duplicate term + scope returns 409 ---


def test_probe_168_duplicate_active_tuple_409(cp_client: TestClient) -> None:
    body = {
        "source_vocabulary": "v",
        "query_term": "dup-test",
        "target_vocabulary": "b",
        "canonical_term": "X",
        "verified_by": "alex",
        "confidence": 1.0,
        "scope": "dup",
    }
    r1 = cp_client.post("/verified_synonyms/", json=body)
    assert r1.status_code == 200
    body["canonical_term"] = "Y"  # different content, same identity tuple
    r2 = cp_client.post("/verified_synonyms/", json=body)
    assert r2.status_code == 409


# --- Probe 169: /verified_synonyms create with NULL scope, twice, second 409 ---


def test_probe_169_duplicate_null_scope_409(cp_client: TestClient) -> None:
    body = {
        "source_vocabulary": "v",
        "query_term": "null-scope-dup",
        "target_vocabulary": "b",
        "canonical_term": "X",
        "verified_by": "alex",
        "confidence": 1.0,
    }
    r1 = cp_client.post("/verified_synonyms/", json=body)
    assert r1.status_code == 200
    body["canonical_term"] = "Y"
    r2 = cp_client.post("/verified_synonyms/", json=body)
    assert r2.status_code == 409


# --- Probe 170: Run.status enum — only valid values stored ---


def test_probe_170_run_status_enum_round_trip(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import Run as RunORM
    from apecx_integration.control_plane.schemas.enums import RunStatus

    factory = make_session_factory(cp_engine)
    rid = uuid4()
    with factory() as session:
        session.add(RunORM(
            id=rid,
            user_id="alex",
            status=RunStatus.PAUSED,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    with factory() as session:
        r = session.get(RunORM, rid)
        assert r.status is RunStatus.PAUSED


# --- Probe 171: Step.status enum round-trip ---


def test_probe_171_step_status_enum(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        Step as StepORM,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind,
        StepStatus,
    )

    factory = make_session_factory(cp_engine)
    run_id = _seed_run(cp_engine)
    sid = uuid4()
    with factory() as session:
        session.add(StepORM(
            id=sid,
            run_id=run_id,
            step_name="t",
            executor=ExecutorKind.LOCAL,
            status=StepStatus.SKIPPED,
            input_artifact_ids=[],
            output_artifact_ids=[],
            created_at=datetime.now(UTC),
        ))
        session.commit()
    with factory() as session:
        s = session.get(StepORM, sid)
        assert s.status is StepStatus.SKIPPED


# --- Probe 172: Approval.kind enum round-trip ---


def test_probe_172_approval_kind_enum(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        Approval as ApprovalORM,
        Step as StepORM,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ApprovalKind,
        ExecutorKind,
        StepStatus,
    )

    factory = make_session_factory(cp_engine)
    run_id = _seed_run(cp_engine)
    sid = uuid4()
    aid = uuid4()
    with factory() as session:
        session.add(StepORM(
            id=sid,
            run_id=run_id,
            step_name="t",
            executor=ExecutorKind.LOCAL,
            status=StepStatus.PENDING,
            input_artifact_ids=[],
            output_artifact_ids=[],
            created_at=datetime.now(UTC),
        ))
        session.add(ApprovalORM(
            id=aid,
            step_id=sid,
            kind=ApprovalKind.SILENT,
            policy={},
            created_at=datetime.now(UTC),
        ))
        session.commit()
    with factory() as session:
        a = session.get(ApprovalORM, aid)
        assert a.kind is ApprovalKind.SILENT


# --- Probe 173: /verified_synonyms revoke + lookup returns null even for the revoked tuple ---


def test_probe_173_lookup_after_revoke_null(cp_client: TestClient) -> None:
    body = {
        "source_vocabulary": "v",
        "query_term": "rev-look",
        "target_vocabulary": "b",
        "canonical_term": "X",
        "verified_by": "alex",
        "confidence": 1.0,
        "scope": "rl",
    }
    cr = cp_client.post("/verified_synonyms/", json=body)
    sid = cr.json()["verified_synonym"]["id"]
    cp_client.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex", "revocation_reason": "rev"},
    )
    r = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "v",
            "target_vocabulary": "b",
            "query_terms": ["rev-look"],
            "scope": "rl",
        },
    )
    assert r.json()["matches"][0]["result"] is None


# --- Probe 174: Concurrent recorder.validate by 5 threads on same chain ---


def test_probe_174_concurrent_validate(cp_engine: Engine) -> None:
    import threading

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    for i in range(10):
        recorder.record(
            run_id=run_id, event_type=ProvenanceEventType.STEP_COMPLETED,
            actor="p", payload={"i": i},
        )
    errors: list[BaseException] = []

    def _run():
        try:
            recorder.validate(run_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors


# --- Probe 175: Recorder hash includes payload (different payload → different hash) ---


def test_probe_175_payload_changes_hash(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.provenance.recorder import _compute_event_hash
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType

    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    rid = uuid4()
    h1 = _compute_event_hash(
        prev_event_hash=None,
        run_id=rid,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="p",
        timestamp=fixed,
        payload={"v": "a"},
    )
    h2 = _compute_event_hash(
        prev_event_hash=None,
        run_id=rid,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="p",
        timestamp=fixed,
        payload={"v": "b"},
    )
    assert h1 != h2
