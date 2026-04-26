"""Probe batch 4 — scale, large payloads, unusual input shapes.

Probes 101-125. Each test is one distinct adversarial probe.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from apecx_integration.control_plane.app import create_app
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


# --- Probe 101: ArtifactStore.store with 10MB content ---


def test_probe_101_artifact_store_10mb(cp_engine: Engine, tmp_path) -> None:
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory, recorder=recorder, root=tmp_path / "art"
    )
    run_id = _seed_run(cp_engine)
    big = b"a" * (10 * 1024 * 1024)
    a = store.store(content=big, kind=ArtifactKind.OUTPUT, run_id=run_id, mime_type="application/octet-stream")
    assert store.load_content(a.id) == big


# --- Probe 102: 1KB payload in a provenance event ---


def test_probe_102_recorder_payload_1kb(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    payload = {"large": "x" * 1024}
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload=payload,
    )
    assert evt.payload == payload


# --- Probe 103: 100KB payload ---


def test_probe_103_recorder_payload_100kb(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    payload = {"large": "x" * 102_400}
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload=payload,
    )
    recorder.validate(run_id)
    assert evt.payload["large"] == "x" * 102_400


# --- Probe 104: payload with many keys (1000 keys) ---


def test_probe_104_recorder_payload_many_keys(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    payload = {f"k{i}": f"v{i}" for i in range(1000)}
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload=payload,
    )
    assert len(evt.payload) == 1000


# --- Probe 105: payload with unicode keys + values ---


def test_probe_105_recorder_payload_unicode(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    payload = {"键": "值", "🧬": "🔬"}
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload=payload,
    )
    assert evt.payload == payload


# --- Probe 106: Run.user_id with mixed-case ---


def test_probe_106_user_id_case_sensitivity(cp_engine: Engine, cp_client: TestClient) -> None:
    """user_id is case-sensitive (matches std SQL behavior)."""
    _seed_run(cp_engine, user_id="Alex")
    _seed_run(cp_engine, user_id="alex")
    r1 = cp_client.post("/runs/list", json={"user_id": "Alex", "limit": 10})
    r2 = cp_client.post("/runs/list", json={"user_id": "alex", "limit": 10})
    assert len(r1.json()["runs"]) == 1
    assert len(r2.json()["runs"]) == 1
    # Different runs returned
    assert r1.json()["runs"][0]["id"] != r2.json()["runs"][0]["id"]


# --- Probe 107: very long user_id (1000 chars) ---


def test_probe_107_long_user_id(cp_engine: Engine, cp_client: TestClient) -> None:
    """user_id is String(255) — let's see what happens with longer."""
    long_uid = "a" * 1000
    _seed_run(cp_engine, user_id=long_uid)
    r = cp_client.post("/runs/list", json={"user_id": long_uid, "limit": 10})
    # SQLite TEXT doesn't enforce length. Should round-trip.
    assert r.status_code == 200


# --- Probe 108: /runs/list for nonexistent user returns empty list ---


def test_probe_108_runs_list_unknown_user(cp_client: TestClient) -> None:
    r = cp_client.post("/runs/list", json={"user_id": "nobody-here", "limit": 10})
    assert r.status_code == 200
    assert r.json()["runs"] == []


# --- Probe 109: /metrics/approvals with since= in the past ---


def test_probe_109_metrics_past_since_works(cp_client: TestClient) -> None:
    r = cp_client.get("/metrics/approvals?since=2020-01-01T00:00:00Z")
    assert r.status_code == 200


# --- Probe 110: /verified_synonyms/lookup with empty source returns 422 ---


def test_probe_110_lookup_empty_source(cp_client: TestClient) -> None:
    r = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "",
            "target_vocabulary": "bvbrc",
            "query_terms": ["x"],
        },
    )
    assert r.status_code == 422


# --- Probe 111: /verified_synonyms create with very long canonical_term ---


def test_probe_111_create_long_canonical_term(cp_client: TestClient) -> None:
    long_term = "x" * 600  # > 512 char schema String limit
    r = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v",
            "query_term": "long-term-test",
            "target_vocabulary": "b",
            "canonical_term": long_term,
            "verified_by": "alex",
            "confidence": 1.0,
            "scope": "long",
        },
    )
    # SQLite doesn't enforce String length; row goes through.
    # Probe documents this behavior — not strictly a bug.
    assert r.status_code in (200, 422)


# --- Probe 112: /metrics/approvals doesn't 500 on empty DB ---


def test_probe_112_metrics_empty_db(cp_client: TestClient) -> None:
    r = cp_client.get("/metrics/approvals?since=2020-01-01T00:00:00Z")
    assert r.status_code == 200


# --- Probe 113: /workflows/start with description=" " (single space) ---


def test_probe_113_workflows_start_single_space_description(cp_client: TestClient) -> None:
    """min_length=1 accepts single space; route returns 503 for unconfigured."""
    r = cp_client.post(
        "/workflows/start",
        json={"description": " ", "user_id": "alex"},
    )
    assert r.status_code != 500


# --- Probe 114: GET /approvals/{id} returns full Approval shape ---


def test_probe_114_approvals_get_after_create(
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
    g = cp_client.get(f"/approvals/{aid}")
    assert g.status_code == 200
    body = g.json()["approval"]
    assert body["id"] == aid
    assert body["status"] == "pending"


# --- Probe 115: Approval.policy is preserved across approve ---


def test_probe_115_approval_policy_preserved(
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
            "summary": "summary-text",
            "artifact_ids": [],
            "policy": {"reviewer_role": "lead"},
        },
    )
    aid = cr.json()["approval"]["id"]
    cp_client.post(
        "/approvals/approve",
        json={"approval_id": aid, "decided_by": "alex"},
    )
    g = cp_client.get(f"/approvals/{aid}")
    body = g.json()["approval"]
    # policy should still contain reviewer_role
    assert body["policy"]["reviewer_role"] == "lead"
    assert body["policy"]["summary"] == "summary-text"


# --- Probe 116: /verified_synonyms revoke with revocation_reason="" rejected ---


def test_probe_116_revoke_empty_reason_rejected(cp_client: TestClient) -> None:
    cr = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v",
            "query_term": "empty-reason-test",
            "target_vocabulary": "b",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.0,
            "scope": "er",
        },
    )
    sid = cr.json()["verified_synonym"]["id"]
    r = cp_client.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex", "revocation_reason": ""},
    )
    assert r.status_code == 422  # min_length=1


# --- Probe 117: 500ms after recorder.record, validate is fast ---


def test_probe_117_validate_perf_after_record(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    for i in range(20):
        recorder.record(
            run_id=run_id,
            event_type=ProvenanceEventType.STEP_COMPLETED,
            actor="probe",
            payload={"i": i},
        )
    import time
    start = time.time()
    recorder.validate(run_id)
    elapsed = time.time() - start
    # 20-event chain should validate in < 500ms easily
    assert elapsed < 0.5, f"PROBE 117: validate too slow ({elapsed:.2f}s)"


# --- Probe 118: Recorder with multiple distinct runs in cache ---


def test_probe_118_recorder_cache_multi_run(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    runs = [_seed_run(cp_engine) for _ in range(5)]
    for r in runs:
        recorder.record(
            run_id=r,
            event_type=ProvenanceEventType.RUN_STARTED,
            actor="probe",
            payload={},
        )
    assert len(recorder._last_hash) == 5
    for r in runs:
        recorder.validate(r)


# --- Probe 119: confirm_allocation with confirmed_core_hours = 0.0 ---


def test_probe_119_hpc_confirm_zero_core_hours(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """0.0 confirmed should be 422 if estimated > 0."""
    run_id = _seed_run(cp_engine)
    from apecx_integration.control_plane.models.entities import (
        AllocationEstimate as AEORM,
    )
    factory = make_session_factory(cp_engine)
    with factory() as session:
        session.add(
            AEORM(
                id=uuid4(),
                run_id=run_id,
                estimated_core_hours=10.0,
                estimated_wall_time_seconds=36000.0,
                endpoint="polaris",
                user_confirmed=False,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    r = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(run_id), "confirmed_core_hours": 0.0},
    )
    assert r.status_code == 422


# --- Probe 120: /workflows/diff for run with no GeneratedArtifact metadata ---


def test_probe_120_diff_missing_generated_artifact(
    cp_engine: Engine, cp_client: TestClient, tmp_path
) -> None:
    """Run has workflow_config_id pointing at an Artifact, but no
    GeneratedArtifact sidecar — route returns 422."""
    run_id = uuid4()
    art_id = uuid4()
    yaml_path = tmp_path / "wf.yml"
    yaml_path.write_text("name: x\nsteps: {}\n", encoding="utf-8")
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, "
                "'sha', 1, 'application/x-yaml', :ts)"
            ),
            {
                "id": str(art_id),
                "rid": str(run_id),
                "loc": str(yaml_path),
                "ts": now,
            },
        )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :a WHERE id = :r"),
            {"a": str(art_id), "r": str(run_id)},
        )
    r = cp_client.post("/workflows/diff", json={"run_id": str(run_id)})
    assert r.status_code == 422


# --- Probe 121: SQL ORDER BY survives many same-microsecond runs ---


def test_probe_121_runs_list_ordering_under_tied_microseconds(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """Insert 5 runs with the SAME created_at — list should still
    return them all without duplicating or losing rows."""
    fixed_ts = datetime.now(UTC).isoformat()
    rids = [uuid4() for _ in range(5)]
    with cp_engine.begin() as conn:
        for rid in rids:
            conn.execute(
                text(
                    "INSERT INTO run (id, user_id, status, created_at) "
                    "VALUES (:id, 'alex', 'PENDING', :ts)"
                ),
                {"id": str(rid), "ts": fixed_ts},
            )
    r = cp_client.post("/runs/list", json={"user_id": "alex", "limit": 100})
    returned = {UUID(rr["id"]) for rr in r.json()["runs"]}
    assert returned == set(rids), (
        f"PROBE 121: tied-microsecond runs lost or duplicated: "
        f"expected {set(rids)}, got {returned}"
    )


# --- Probe 122: Recorder.record with explicit now= preserves the timestamp ---


def test_probe_122_recorder_explicit_now(cp_engine: Engine) -> None:
    fixed = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={},
        now=fixed,
    )
    # Read back via DB
    with cp_engine.connect() as conn:
        row = conn.execute(
            text("SELECT timestamp FROM provenance_event WHERE id = :id"),
            {"id": str(evt.id)},
        ).scalar_one()
    # Round-tripped timestamp matches (modulo tz strip on SQLite).
    parsed = datetime.fromisoformat(str(row).replace(" ", "T")) if isinstance(row, str) else row
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    assert parsed == fixed


# --- Probe 123: VerifiedSynonym lookup matches no-active-row term ---


def test_probe_123_lookup_term_with_only_revoked(cp_client: TestClient) -> None:
    """Term with only an inactive (revoked) row → match is null."""
    body = {
        "source_vocabulary": "v",
        "query_term": "only-revoked-test",
        "target_vocabulary": "b",
        "canonical_term": "X",
        "verified_by": "alex",
        "confidence": 1.0,
        "scope": "rev",
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
            "query_terms": ["only-revoked-test"],
            "scope": "rev",
        },
    )
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert matches[0]["result"] is None


# --- Probe 124: /workflows/start without body fields fails 422 ---


def test_probe_124_workflows_start_missing_fields(cp_client: TestClient) -> None:
    r = cp_client.post("/workflows/start", json={})
    assert r.status_code == 422


# --- Probe 125: /workflows/start with extra unrecognized field rejected ---


def test_probe_125_workflows_start_extra_field(cp_client: TestClient) -> None:
    """_APIBase has extra='forbid'; unrecognized fields rejected."""
    r = cp_client.post(
        "/workflows/start",
        json={
            "description": "test",
            "user_id": "alex",
            "extra_field": "should-be-rejected",
        },
    )
    assert r.status_code == 422
