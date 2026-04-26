"""Probe batch 3 — concurrency + ordering edge cases.

Probes 76-100. Each test is one distinct adversarial probe.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.notifications.sweeper import RunStateSweeper
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import (
    ApprovalStatus,
    ProvenanceEventType,
)
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def _seed_run(engine: Engine, *, status_value: str = "PENDING") -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', :st, :ts)"
            ),
            {"id": str(run_id), "st": status_value, "ts": now},
        )
    return run_id


# --- Probe 76: K=50 concurrent recorder appends produce K events ---


async def test_probe_76_k50_recorder_appends(cp_engine: Engine) -> None:
    """Cluster X covered K=20. Push to K=50 — same chain integrity
    expected."""
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    K = 50

    async def _append(idx: int) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: recorder.record(
                run_id=run_id,
                event_type=ProvenanceEventType.STEP_COMPLETED,
                actor="probe",
                payload={"i": idx},
            ),
        )

    await asyncio.wait_for(
        asyncio.gather(*[_append(i) for i in range(K)]),
        timeout=30.0,
    )
    with cp_engine.connect() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM provenance_event WHERE run_id = :rid"
            ),
            {"rid": str(run_id)},
        ).scalar_one()
    assert cnt == K, f"PROBE 76: lost some events under K=50 concurrency: {cnt}/{K}"
    recorder.validate(run_id)


# --- Probe 77: K=50 concurrent verified_synonyms creates with unique scopes ---


async def test_probe_77_k50_create_unique_scopes(cp_engine: Engine) -> None:
    """Concurrency on /verified_synonyms create with DISTINCT
    (source, query, target, scope) tuples. All K should succeed."""
    app = create_app(engine=cp_engine)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    K = 50

    async def _create(i: int):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            return await ac.post(
                "/verified_synonyms/",
                json={
                    "source_vocabulary": "violin",
                    "query_term": "stress",
                    "target_vocabulary": "bvbrc",
                    "canonical_term": f"X-{i}",
                    "verified_by": "alex",
                    "confidence": 1.0,
                    "scope": f"unique-{i}",
                },
            )

    responses = await asyncio.gather(*[_create(i) for i in range(K)])
    statuses = [r.status_code for r in responses]
    assert all(s == 200 for s in statuses), (
        f"PROBE 77: K=50 unique-scope creates not all 200: {[s for s in statuses if s != 200]}"
    )


# --- Probe 78: Sweeper sweeps N=20 stale runs in one call ---


def test_probe_78_sweeper_handles_n20_stale(cp_engine: Engine) -> None:
    """Sweeper with N=20 stale runs — all should be transitioned in
    one sweep() call."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    sweeper = RunStateSweeper(factory, recorder)
    N = 20
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    run_ids = []
    with cp_engine.begin() as conn:
        for _ in range(N):
            rid = uuid4()
            conn.execute(
                text(
                    "INSERT INTO run (id, user_id, status, created_at) "
                    "VALUES (:id, 'alex', 'RUNNING', :ts)"
                ),
                {"id": str(rid), "ts": long_ago.isoformat()},
            )
            run_ids.append(rid)
    results = sweeper.sweep()
    assert len(results) == N
    with cp_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status FROM run WHERE id IN :ids").bindparams(
                __import__("sqlalchemy").bindparam("ids", expanding=True)
            ),
            {"ids": [str(r) for r in run_ids]},
        ).fetchall()
    assert all(r[0] == "FAILED" for r in rows)


# --- Probe 79: Sweeper threshold == cutoff exactly (boundary) ---


def test_probe_79_sweeper_threshold_boundary(cp_engine: Engine) -> None:
    """Run with most_recent EXACTLY equal to cutoff — boundary
    behavior is 'NOT stale' (>= cutoff is the not-stale condition)."""
    now = datetime.now(UTC)
    threshold = timedelta(minutes=15)
    run_id = uuid4()
    # created_at exactly at the cutoff = now - threshold
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts)"
            ),
            {
                "id": str(run_id),
                "ts": (now - threshold).isoformat(),
            },
        )
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    sweeper = RunStateSweeper(factory, recorder)
    results = sweeper.sweep(stale_after=threshold, now=now)
    # Boundary: not swept
    assert len(results) == 0


# --- Probe 80: Sweeper finds run that's 1 microsecond past cutoff ---


def test_probe_80_sweeper_just_past_cutoff(cp_engine: Engine) -> None:
    now = datetime.now(UTC)
    threshold = timedelta(minutes=15)
    run_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts)"
            ),
            {
                "id": str(run_id),
                "ts": (now - threshold - timedelta(microseconds=1)).isoformat(),
            },
        )
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    sweeper = RunStateSweeper(factory, recorder)
    results = sweeper.sweep(stale_after=threshold, now=now)
    assert len(results) == 1
    assert results[0].run_id == run_id


# --- Probe 81: Recorder validate detects multiple genesis events ---


def test_probe_81_validate_detects_multiple_genesis(cp_engine: Engine) -> None:
    run_id = _seed_run(cp_engine)
    # Insert two events with prev_event_hash=NULL (both claim
    # genesis).
    e1 = uuid4()
    e2 = uuid4()
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        for eid in (e1, e2):
            conn.execute(
                text(
                    "INSERT INTO provenance_event (id, run_id, event_type, "
                    "actor, timestamp, payload, prev_event_hash, event_hash) "
                    "VALUES (:id, :rid, 'STEP_COMPLETED', 'probe', :ts, "
                    "'{}', NULL, :h)"
                ),
                {
                    "id": str(eid),
                    "rid": str(run_id),
                    "ts": now,
                    "h": str(eid).replace("-", "")[:64].ljust(64, "0"),
                },
            )
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    from apecx_integration.control_plane.provenance.recorder import ChainBroken

    with pytest.raises(ChainBroken):
        recorder.validate(run_id)


# --- Probe 82: ArtifactStore.store + load_content round-trip ---


def test_probe_82_artifact_store_round_trip(cp_engine: Engine, tmp_path) -> None:
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory,
        recorder=recorder,
        root=tmp_path / "art",
    )
    run_id = _seed_run(cp_engine)
    content = b"hello world"
    a = store.store(
        content=content, kind=ArtifactKind.INPUT, run_id=run_id, mime_type="text/plain"
    )
    assert store.load_content(a.id) == content


# --- Probe 83: ArtifactStore detects on-disk corruption ---


def test_probe_83_artifact_store_corrupted_disk(
    cp_engine: Engine, tmp_path
) -> None:
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory,
        recorder=recorder,
        root=tmp_path / "art",
    )
    run_id = _seed_run(cp_engine)
    a = store.store(
        content=b"original", kind=ArtifactKind.INPUT, run_id=run_id, mime_type="text/plain"
    )
    from pathlib import Path
    Path(a.location).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        store.load_content(a.id)


# --- Probe 84: Two artifact_store stores with same content produce 2 distinct rows ---


def test_probe_84_artifact_store_dedupe_disabled(cp_engine: Engine, tmp_path) -> None:
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory,
        recorder=recorder,
        root=tmp_path / "art",
    )
    run_id = _seed_run(cp_engine)
    a1 = store.store(
        content=b"same", kind=ArtifactKind.INPUT, run_id=run_id, mime_type="text/plain"
    )
    a2 = store.store(
        content=b"same", kind=ArtifactKind.INPUT, run_id=run_id, mime_type="text/plain"
    )
    assert a1.id != a2.id
    assert a1.content_hash == a2.content_hash


# --- Probe 85: Migration round-trip 0001..head and back ---


def test_probe_85_migration_full_round_trip(tmp_path) -> None:
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "rt.db"
    url = f"sqlite:///{db_file}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(Path("migrations").resolve()))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")  # re-upgrade from clean


# --- Probe 86: /verified_synonyms create with same content → distinct rows ---


def test_probe_86_verified_synonyms_distinct_per_scope(cp_client: TestClient) -> None:
    """Same (source, query, target) but different scopes can coexist."""
    body = lambda scope: {
        "source_vocabulary": "violin",
        "query_term": "multi-scope",
        "target_vocabulary": "bvbrc",
        "canonical_term": "X",
        "verified_by": "alex",
        "confidence": 1.0,
        "scope": scope,
    }
    r1 = cp_client.post("/verified_synonyms/", json=body("scope1"))
    r2 = cp_client.post("/verified_synonyms/", json=body("scope2"))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["verified_synonym"]["id"] != r2.json()["verified_synonym"]["id"]


# --- Probe 87: /verified_synonyms create + revoke + create same tuple ---


def test_probe_87_verified_synonyms_idempotent_after_revoke(
    cp_client: TestClient,
) -> None:
    body = {
        "source_vocabulary": "violin",
        "query_term": "rev-create",
        "target_vocabulary": "bvbrc",
        "canonical_term": "V1",
        "verified_by": "alex",
        "confidence": 1.0,
        "scope": "rc",
    }
    r1 = cp_client.post("/verified_synonyms/", json=body)
    sid = r1.json()["verified_synonym"]["id"]
    rev = cp_client.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex", "revocation_reason": "rev"},
    )
    assert rev.status_code == 200
    body["canonical_term"] = "V2"
    r2 = cp_client.post("/verified_synonyms/", json=body)
    assert r2.status_code == 200


# --- Probe 88: Concurrent /verified_synonyms create (different terms) ---


async def test_probe_88_concurrent_create_distinct_terms(cp_engine: Engine) -> None:
    app = create_app(engine=cp_engine)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    N = 30

    async def _create(i: int):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            return await ac.post(
                "/verified_synonyms/",
                json={
                    "source_vocabulary": "violin",
                    "query_term": f"distinct-{i}",
                    "target_vocabulary": "bvbrc",
                    "canonical_term": f"X-{i}",
                    "verified_by": "alex",
                    "confidence": 1.0,
                    "scope": "distinct",
                },
            )

    rs = await asyncio.gather(*[_create(i) for i in range(N)])
    assert all(r.status_code == 200 for r in rs)


# --- Probe 89: /runs/list returns runs in created_at DESC order ---


def test_probe_89_runs_list_ordered_desc(cp_engine: Engine, cp_client: TestClient) -> None:
    import time
    ids = []
    for _ in range(5):
        ids.append(_seed_run(cp_engine))
        time.sleep(0.001)
    resp = cp_client.post("/runs/list", json={"user_id": "alex", "limit": 10})
    assert resp.status_code == 200
    returned = [UUID(r["id"]) for r in resp.json()["runs"]]
    # Newest-first order
    assert returned == list(reversed(ids))


# --- Probe 90: /runs/status returns step list ordered by created_at ---


def test_probe_90_runs_status_steps_ordered(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    import time
    run_id = _seed_run(cp_engine)
    step_ids = []
    for _ in range(3):
        sid = uuid4()
        with cp_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO step (id, run_id, step_name, executor, "
                    "status, input_artifact_ids, output_artifact_ids, "
                    "created_at) VALUES (:id, :rid, 's', 'LOCAL', "
                    "'PENDING', '[]', '[]', :ts)"
                ),
                {
                    "id": str(sid),
                    "rid": str(run_id),
                    "ts": datetime.now(UTC).isoformat(),
                },
            )
        step_ids.append(sid)
        time.sleep(0.001)
    resp = cp_client.post("/runs/status", json={"run_id": str(run_id)})
    assert resp.status_code == 200
    returned = [UUID(s["id"]) for s in resp.json()["steps"]]
    assert returned == step_ids


# --- Probe 91: Recorder cluster X cache survives within an instance ---


def test_probe_91_recorder_cache_persists_in_instance(cp_engine: Engine) -> None:
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    e1 = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={"i": 1},
    )
    # Cache should now have run_id.
    assert run_id in recorder._last_hash
    assert recorder._last_hash[run_id] == e1.event_hash


# --- Probe 92: Recorder hash is deterministic ---


def test_probe_92_hash_determinism(cp_engine: Engine) -> None:
    """Same input → same hash. Cluster X invariant."""
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    r1 = ProvenanceRecorder(factory)
    r2 = ProvenanceRecorder(factory)
    fixed_ts = datetime(2026, 1, 1, tzinfo=UTC)
    e_a = r1.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={"k": "v"},
        now=fixed_ts,
    )
    # Inserting the same event again would conflict on FK / unique
    # check via the chain. Instead, recompute the hash via the same
    # function.
    from apecx_integration.control_plane.provenance.recorder import _compute_event_hash
    h2 = _compute_event_hash(
        prev_event_hash=None,
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        timestamp=fixed_ts,
        payload={"k": "v"},
    )
    assert e_a.event_hash == h2


# --- Probe 93: ApprovalStep run/step run_id mismatch returns 400 ---


def test_probe_93_create_approval_run_step_mismatch_v2(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    run_a = _seed_run(cp_engine)
    run_b = _seed_run(cp_engine)
    step_a = uuid4()
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, "
                "status, input_artifact_ids, output_artifact_ids, "
                "created_at) VALUES (:id, :rid, 's', 'LOCAL', "
                "'PAUSED_FOR_APPROVAL', '[]', '[]', :ts)"
            ),
            {"id": str(step_a), "rid": str(run_a), "ts": now},
        )
    resp = cp_client.post(
        "/approvals/",
        json={
            "run_id": str(run_b),
            "step_id": str(step_a),
            "kind": "soft",
            "summary": "test",
            "artifact_ids": [],
        },
    )
    assert resp.status_code == 400


# --- Probe 94: /approvals/approve a previously-rejected approval is 409 ---


def test_probe_94_approve_after_reject_409(
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
            "run_id": str(run_id),
            "step_id": str(step_id),
            "kind": "hard",
            "summary": "test",
            "artifact_ids": [],
        },
    )
    aid = cr.json()["approval"]["id"]
    rej = cp_client.post(
        "/approvals/reject",
        json={"approval_id": aid, "decided_by": "alex", "reason": "no"},
    )
    assert rej.status_code == 200
    app_resp = cp_client.post(
        "/approvals/approve",
        json={"approval_id": aid, "decided_by": "alex"},
    )
    assert app_resp.status_code == 409


# --- Probe 95: /approvals/correct a previously-approved is 409 ---


def test_probe_95_correct_after_approve_409(
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
            "run_id": str(run_id),
            "step_id": str(step_id),
            "kind": "hard",
            "summary": "test",
            "artifact_ids": [],
        },
    )
    aid = cr.json()["approval"]["id"]
    cp_client.post(
        "/approvals/approve",
        json={"approval_id": aid, "decided_by": "alex"},
    )
    cor = cp_client.post(
        "/approvals/correct",
        json={
            "approval_id": aid,
            "decided_by": "alex",
            "modifications": {"x": "y"},
        },
    )
    assert cor.status_code == 409


# --- Probe 96: /healthz returns 200 with status:ok ---


def test_probe_96_healthz_returns_ok(cp_client: TestClient) -> None:
    resp = cp_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --- Probe 97: /metrics/approvals with empty window ---


def test_probe_97_metrics_empty_window(cp_client: TestClient) -> None:
    resp = cp_client.get("/metrics/approvals?since=2050-01-01T00:00:00Z")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0


# --- Probe 98: /metrics/approvals on far-future since returns zero counts ---


def test_probe_98_metrics_future_since_zero(cp_client: TestClient) -> None:
    resp = cp_client.get("/metrics/approvals?since=2099-01-01T00:00:00Z")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["percent_auto_approved"] == 0.0


# --- Probe 99: /workflows/start description with literal newlines ---


def test_probe_99_workflows_start_description_with_newlines(
    cp_client: TestClient,
) -> None:
    resp = cp_client.post(
        "/workflows/start",
        json={
            "description": "line1\nline2\r\nline3\tend",
            "user_id": "alex",
        },
    )
    # Composer not configured = 503; route doesn't 500 on newlines.
    assert resp.status_code != 500


# --- Probe 100: Recorder hash chain links across two runs are independent ---


def test_probe_100_chains_independent_per_run(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    r1 = _seed_run(cp_engine)
    r2 = _seed_run(cp_engine)
    e1 = recorder.record(
        run_id=r1,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={"r": 1},
    )
    e2 = recorder.record(
        run_id=r2,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={"r": 2},
    )
    # Each chain has its own genesis; e2.prev should be None (not e1.hash)
    assert e1.prev_event_hash is None
    assert e2.prev_event_hash is None
    recorder.validate(r1)
    recorder.validate(r2)
