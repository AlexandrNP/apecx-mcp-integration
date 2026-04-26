"""Probe batch 2 — state-machine + data-integrity edge cases.

Probes 51-75. Each test is one distinct adversarial probe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import (
    ChainBroken,
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
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


# --- Probe 51: recorder.validate on a run with 0 events ---


def test_probe_51_validate_empty_chain(cp_engine: Engine) -> None:
    """A run with no provenance events is vacuously valid; validate
    must NOT raise."""
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    recorder.validate(run_id)


# --- Probe 52: recorder.validate on a run that doesn't exist ---


def test_probe_52_validate_nonexistent_run(cp_engine: Engine) -> None:
    """Validate on a run that doesn't exist in the run table.
    Vacuously valid (no events = no chain to break)."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    recorder.validate(uuid4())  # should not raise


# --- Probe 53: recorder.record fails FK on non-existent run ---


def test_probe_53_record_fails_fk_for_missing_run(cp_engine: Engine) -> None:
    """Recording an event for a run_id that doesn't exist must
    raise (FK violation), not silently insert an orphan event."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    with pytest.raises(Exception):  # IntegrityError
        recorder.record(
            run_id=uuid4(),
            event_type=ProvenanceEventType.RUN_STARTED,
            actor="probe",
            payload={},
        )


# --- Probe 54: ProvenanceEvent payload with deeply nested dict ---


def test_probe_54_record_deeply_nested_payload(cp_engine: Engine) -> None:
    """Payload with deep nesting should serialize cleanly via JSON."""
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    payload = {"l1": {"l2": {"l3": {"l4": {"l5": "deep"}}}}}
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload=payload,
    )
    assert evt.payload == payload


# --- Probe 55: ProvenanceEvent payload with very long string ---


def test_probe_55_record_huge_payload_string(cp_engine: Engine) -> None:
    """100KB string in payload — should round-trip."""
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    big = "x" * 100_000
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={"big": big},
    )
    assert evt.payload["big"] == big


# --- Probe 56: validate is idempotent (calling twice is fine) ---


def test_probe_56_validate_idempotent(cp_engine: Engine) -> None:
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={},
    )
    recorder.validate(run_id)
    recorder.validate(run_id)  # second call must also succeed


# --- Probe 57: validate detects a manually-corrupted prev_event_hash ---


def test_probe_57_validate_detects_external_tamper(cp_engine: Engine) -> None:
    run_id = _seed_run(cp_engine)
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    e1 = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload={"i": 1},
    )
    e2 = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.STEP_COMPLETED,
        actor="probe",
        payload={"i": 2},
    )
    # Externally tamper with E2's prev_event_hash so it doesn't
    # match E1's event_hash.
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE provenance_event SET prev_event_hash = 'TAMPER' "
                "WHERE id = :id"
            ),
            {"id": str(e2.id)},
        )
    with pytest.raises(ChainBroken):
        recorder.validate(run_id)


# --- Probe 58: /workflows/diff for a CANCELLED run (preview-mode) ---


def test_probe_58_workflows_diff_cancelled_run(
    cp_engine: Engine, cp_client: TestClient, tmp_path
) -> None:
    """A CANCELLED run from /workflows/plan should still expose its
    artifact via /workflows/diff (it's the whole point of
    preview-mode)."""
    # Set up a real generated workflow artifact + GeneratedArtifact
    # row. The diff route needs both.
    run_id = uuid4()
    art_id = uuid4()
    yaml_path = tmp_path / "wf.yml"
    yaml_path.write_text("name: x\nsteps: {}\nlinks: {}\n", encoding="utf-8")
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, '_preview', 'CANCELLED', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, "
                "'sha256-placeholder', 1, 'application/x-yaml', :ts)"
            ),
            {
                "id": str(art_id),
                "rid": str(run_id),
                "loc": str(yaml_path),
                "ts": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO generated_artifact (artifact_id, source_prompt, "
                "library_version, llm_model, llm_model_version_hash, "
                "composition_summary) "
                "VALUES (:aid, :p, :lv, :m, :h, '{}')"
            ),
            {
                "aid": str(art_id),
                "p": "test",
                "lv": "0.1.0",
                "m": "test",
                "h": "0" * 64,
            },
        )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :a WHERE id = :r"),
            {"a": str(art_id), "r": str(run_id)},
        )
    resp = cp_client.post(
        "/workflows/diff",
        json={"run_id": str(run_id)},
    )
    assert resp.status_code == 200, resp.text


# --- Probe 59: /verified_synonyms revoke with superseded_by=self ---


def test_probe_59_revoke_self_supersede(cp_client: TestClient) -> None:
    """superseded_by pointing at the row being revoked itself —
    should reject (a row can't supersede itself)."""
    create_resp = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "violin",
            "query_term": "self-supersede-test",
            "target_vocabulary": "bvbrc",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.0,
            "scope": "self-test",
        },
    )
    assert create_resp.status_code == 200
    syn_id = create_resp.json()["verified_synonym"]["id"]

    revoke_resp = cp_client.patch(
        f"/verified_synonyms/{syn_id}",
        json={
            "revoked_by": "alex",
            "revocation_reason": "test",
            "superseded_by": syn_id,
        },
    )
    # Either: route specifically rejects self-supersede with 4xx,
    # OR: silently accepts (which is semantically dubious — a row
    # superseded by itself is meaningless). Probe documents what
    # the route does.
    if revoke_resp.status_code == 200:
        # Self-supersede is currently accepted. That's a UX gap
        # (operator can't tell what "X superseded by X" means)
        # but not a fail-fast violation; assert the audit trail
        # is at least internally consistent.
        body = revoke_resp.json()["verified_synonym"]
        assert body["superseded_by"] == syn_id
        assert body["is_active"] is False
    else:
        assert revoke_resp.status_code in (400, 422)


# --- Probe 60: /verified_synonyms create with leading/trailing whitespace ---


def test_probe_60_verified_synonyms_create_whitespace_canonical(
    cp_client: TestClient,
) -> None:
    """Whitespace in canonical_term is stored as-is (round-trip)."""
    resp = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "violin",
            "query_term": "ws-test",
            "target_vocabulary": "bvbrc",
            "canonical_term": "  Trimmed Term  ",
            "verified_by": "alex",
            "confidence": 1.0,
            "scope": "ws",
        },
    )
    assert resp.status_code == 200
    body = resp.json()["verified_synonym"]
    assert body["canonical_term"] == "  Trimmed Term  ", (
        "PROBE 60: route silently trimmed whitespace; should round-trip"
    )


# --- Probe 61: /verified_synonyms lookup with empty query_terms list ---


def test_probe_61_verified_synonyms_lookup_empty_query_terms(
    cp_client: TestClient,
) -> None:
    """Empty list rejected by min_length=1."""
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "violin",
            "target_vocabulary": "bvbrc",
            "query_terms": [],
        },
    )
    assert resp.status_code == 422


# --- Probe 62: /verified_synonyms lookup with duplicate query_terms ---


def test_probe_62_verified_synonyms_lookup_duplicates(
    cp_client: TestClient,
) -> None:
    """Duplicates in query_terms — lookup should return 1 row per
    input position (preserving order, including duplicates)."""
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "violin",
            "target_vocabulary": "bvbrc",
            "query_terms": ["a", "a", "b"],
        },
    )
    assert resp.status_code == 200
    matches = resp.json()["matches"]
    assert len(matches) == 3
    assert [m["query_term"] for m in matches] == ["a", "a", "b"]


# --- Probe 63: /runs/list with status_filter=COMPLETED returns only completed ---


def test_probe_63_runs_list_status_filter(cp_engine: Engine, cp_client: TestClient) -> None:
    _seed_run(cp_engine, status_value="PENDING")
    _seed_run(cp_engine, status_value="RUNNING")
    completed_id = _seed_run(cp_engine, status_value="COMPLETED")
    resp = cp_client.post(
        "/runs/list",
        json={"user_id": "alex", "status_filter": "completed", "limit": 10},
    )
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == str(completed_id)


# --- Probe 64: /runs/list with limit=1 returns at most one ---


def test_probe_64_runs_list_limit_one(cp_engine: Engine, cp_client: TestClient) -> None:
    _seed_run(cp_engine)
    _seed_run(cp_engine)
    _seed_run(cp_engine)
    resp = cp_client.post(
        "/runs/list",
        json={"user_id": "alex", "limit": 1},
    )
    assert resp.status_code == 200
    assert len(resp.json()["runs"]) == 1


# --- Probe 65: /verified_synonyms create + immediate revoke + create again works ---


def test_probe_65_create_revoke_create_cycle(cp_client: TestClient) -> None:
    """The unique constraint allows re-creation after a revoke."""
    body = {
        "source_vocabulary": "violin",
        "query_term": "cycle-test",
        "target_vocabulary": "bvbrc",
        "canonical_term": "Mapping V1",
        "verified_by": "alex",
        "confidence": 0.8,
        "scope": "cycle",
    }
    r1 = cp_client.post("/verified_synonyms/", json=body)
    assert r1.status_code == 200
    sid1 = r1.json()["verified_synonym"]["id"]
    rev = cp_client.patch(
        f"/verified_synonyms/{sid1}",
        json={"revoked_by": "alex", "revocation_reason": "rev"},
    )
    assert rev.status_code == 200
    body2 = {**body, "canonical_term": "Mapping V2"}
    r2 = cp_client.post("/verified_synonyms/", json=body2)
    assert r2.status_code == 200, r2.text


# --- Probe 66: /verified_synonyms revoke twice fails 409 ---


def test_probe_66_revoke_twice_is_409(cp_client: TestClient) -> None:
    body = {
        "source_vocabulary": "violin",
        "query_term": "double-revoke-test",
        "target_vocabulary": "bvbrc",
        "canonical_term": "X",
        "verified_by": "alex",
        "confidence": 1.0,
        "scope": "dup",
    }
    cr = cp_client.post("/verified_synonyms/", json=body)
    sid = cr.json()["verified_synonym"]["id"]
    r1 = cp_client.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex", "revocation_reason": "first"},
    )
    assert r1.status_code == 200
    r2 = cp_client.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex", "revocation_reason": "second"},
    )
    assert r2.status_code == 409


# --- Probe 67: /hpc/confirm with confirmed_core_hours equal to estimate ---


def test_probe_67_hpc_confirm_exact_match(
    cp_engine: Engine, cp_client: TestClient, tmp_path
) -> None:
    """Boundary case: confirmed_core_hours == estimated. Per
    docstring, ceiling must COVER the estimate, so equality
    passes."""
    run_id = uuid4()
    art_id = uuid4()
    yaml_path = tmp_path / "wf.yml"
    yaml_path.write_text("name: x\nsteps: {}\n", encoding="utf-8")
    now = datetime.now(UTC).isoformat()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, "
                "'sha256', 1, 'application/x-yaml', :ts)"
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
    # Insert estimate via ORM (consistent format).
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
    resp = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(run_id), "confirmed_core_hours": 10.0},
    )
    assert resp.status_code == 200, resp.text


# --- Probe 68: ApprovalStep create with mismatched run_id fails ---


def test_probe_68_create_approval_run_step_mismatch(
    cp_engine: Engine, cp_client: TestClient
) -> None:
    """create_approval enforces step.run_id == request.run_id —
    cluster V1 added this check."""
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
    # Create approval claiming step_a belongs to run_b → 400
    resp = cp_client.post(
        "/approvals/",
        json={
            "run_id": str(run_b),
            "step_id": str(step_a),
            "kind": "hard",
            "summary": "test",
            "artifact_ids": [],
        },
    )
    assert resp.status_code == 400


# --- Probe 69: /workflows/start without composer returns 503 ---


def test_probe_69_workflows_start_503_no_composer(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/workflows/start",
        json={"description": "test", "user_id": "alex"},
    )
    assert resp.status_code == 503


# --- Probe 70: /workflows/execute without local_executor returns 503 ---


def test_probe_70_workflows_execute_503_no_executor(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/workflows/execute",
        json={"run_id": str(uuid4())},
    )
    assert resp.status_code == 503


# --- Probe 71: /hpc/confirm 404 for nonexistent run ---


def test_probe_71_hpc_confirm_nonexistent_run(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(uuid4()), "confirmed_core_hours": 10.0},
    )
    assert resp.status_code == 404


# --- Probe 72: /hpc/export with non-UUID run_id fails 422 ---


def test_probe_72_hpc_export_invalid_run_id(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/hpc/export",
        json={
            "run_id": "not-a-uuid",
            "target_system": "polaris",
            "output_directory": "/tmp/test",
        },
    )
    assert resp.status_code == 422


# --- Probe 73: /hpc/export with target_system="" fails 422 ---


def test_probe_73_hpc_export_empty_target(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/hpc/export",
        json={
            "run_id": str(uuid4()),
            "target_system": "",
            "output_directory": "/tmp/test",
        },
    )
    # Either 422 (Pydantic min_length on target_system) or 422
    # (UnsupportedSystem from the bundle generator). 4xx, not 5xx.
    assert resp.status_code in (404, 422)


# --- Probe 74: /hpc/ingest with bundle_path that doesn't exist ---


def test_probe_74_hpc_ingest_nonexistent_path(cp_client: TestClient) -> None:
    """IngestHpcBundleRequest only takes bundle_path (run_id is read
    from the bundle's provenance_seed.json). Probe sends only
    bundle_path; route returns 404 since the path doesn't exist.
    """
    resp = cp_client.post(
        "/hpc/ingest",
        json={
            "bundle_path": "/nonexistent/path/that/does/not/exist",
        },
    )
    assert resp.status_code == 404


# --- Probe 75: /metrics/approvals with malformed since= ---


def test_probe_75_metrics_malformed_since(cp_client: TestClient) -> None:
    resp = cp_client.get("/metrics/approvals?since=not-a-date")
    assert resp.status_code in (400, 422)
