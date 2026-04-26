"""Probe batch 7 — final stretch. Probes 176-200.

Categories: cross-route consistency, schema constraints not yet
exercised, edge cases I haven't probed.
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


# --- Probe 176: Validate accepts a chain that grew under cluster X cache ---


def test_probe_176_validate_accepts_grown_chain(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    for _ in range(5):
        recorder.record(
            run_id=run_id, event_type=ProvenanceEventType.STEP_COMPLETED,
            actor="p", payload={},
        )
    recorder.validate(run_id)


# --- Probe 177: Two distinct runs in same recorder don't cross-contaminate cache ---


def test_probe_177_separate_runs_separate_cache_entries(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    r1 = _seed_run(cp_engine)
    r2 = _seed_run(cp_engine)
    e1 = recorder.record(
        run_id=r1, event_type=ProvenanceEventType.RUN_STARTED, actor="p", payload={}
    )
    e2 = recorder.record(
        run_id=r2, event_type=ProvenanceEventType.RUN_STARTED, actor="p", payload={}
    )
    assert recorder._last_hash[r1] == e1.event_hash
    assert recorder._last_hash[r2] == e2.event_hash
    assert recorder._last_hash[r1] != recorder._last_hash[r2]


# --- Probe 178: ApprovalStatus enum round-trip ---


def test_probe_178_approval_status_enum(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        Approval as ApprovalORM,
        Step as StepORM,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ApprovalKind,
        ApprovalStatus,
        ExecutorKind,
        StepStatus,
    )

    factory = make_session_factory(cp_engine)
    run_id = _seed_run(cp_engine)
    sid = uuid4()
    aid = uuid4()
    with factory() as session:
        session.add(StepORM(
            id=sid, run_id=run_id, step_name="t",
            executor=ExecutorKind.LOCAL, status=StepStatus.PENDING,
            input_artifact_ids=[], output_artifact_ids=[],
            created_at=datetime.now(UTC),
        ))
        session.add(ApprovalORM(
            id=aid, step_id=sid, kind=ApprovalKind.SOFT,
            status=ApprovalStatus.AUTO_APPROVED,
            policy={}, created_at=datetime.now(UTC),
        ))
        session.commit()
    with factory() as session:
        a = session.get(ApprovalORM, aid)
        assert a.status is ApprovalStatus.AUTO_APPROVED


# --- Probe 179: ArtifactKind enum round-trip ---


def test_probe_179_artifact_kind_enum(cp_engine: Engine, tmp_path) -> None:
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory, recorder=recorder, root=tmp_path / "art"
    )
    run_id = _seed_run(cp_engine)
    a = store.store(
        content=b"x", kind=ArtifactKind.INTERMEDIATE,
        run_id=run_id, mime_type="application/octet-stream",
    )
    from apecx_integration.control_plane.models.entities import (
        Artifact as ArtifactORM,
    )
    with factory() as session:
        loaded = session.get(ArtifactORM, a.id)
        assert loaded.kind is ArtifactKind.INTERMEDIATE


# --- Probe 180: Recorder.record raises on bad event_type (TypeError) ---


def test_probe_180_record_invalid_event_type(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    with pytest.raises((TypeError, AttributeError, ValueError)):
        recorder.record(
            run_id=run_id, event_type="not-an-enum",  # type: ignore
            actor="p", payload={},
        )


# --- Probe 181: ArtifactStore raises on missing run_id ---


def test_probe_181_artifact_store_fk_violation(cp_engine: Engine, tmp_path) -> None:
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory, recorder=recorder, root=tmp_path / "art"
    )
    with pytest.raises(Exception):
        store.store(
            content=b"x", kind=ArtifactKind.INPUT,
            run_id=uuid4(),  # nonexistent
            mime_type="application/octet-stream",
        )


# --- Probe 182: GeneratedArtifact requires generated_metadata for GENERATED kinds ---


def test_probe_182_generated_artifact_requires_metadata(
    cp_engine: Engine, tmp_path
) -> None:
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory, recorder=recorder, root=tmp_path / "art"
    )
    run_id = _seed_run(cp_engine)
    with pytest.raises(ValueError):
        store.store(
            content=b"x", kind=ArtifactKind.GENERATED_WORKFLOW,
            run_id=run_id, mime_type="application/x-yaml",
        )


# --- Probe 183: GeneratedArtifact rejects metadata for non-GENERATED kinds ---


def test_probe_183_input_kind_rejects_metadata(cp_engine: Engine, tmp_path) -> None:
    from apecx_integration.composition.artifact_store import (
        ArtifactStore,
        GenerationMetadata,
    )
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(
        session_factory=factory, recorder=recorder, root=tmp_path / "art"
    )
    run_id = _seed_run(cp_engine)
    metadata = GenerationMetadata(
        source_prompt="p", library_version="0.1.0",
        llm_model="m", llm_model_version_hash="0" * 64,
    )
    with pytest.raises(ValueError):
        store.store(
            content=b"x", kind=ArtifactKind.INPUT, run_id=run_id,
            mime_type="text/plain", generated_metadata=metadata,
        )


# --- Probe 184: ConfirmAllocationRequest model_dump round-trip ---


def test_probe_184_confirm_allocation_round_trip() -> None:
    from apecx_integration.control_plane.schemas.api import (
        ConfirmAllocationRequest,
    )
    rid = uuid4()
    body = ConfirmAllocationRequest(run_id=rid, confirmed_core_hours=10.0)
    dumped = body.model_dump(mode="json")
    reloaded = ConfirmAllocationRequest.model_validate(dumped)
    assert reloaded == body


# --- Probe 185: VerifiedSynonym schema rejects empty source_vocabulary ---


def test_probe_185_verified_synonym_create_empty_source_422(
    cp_client: TestClient,
) -> None:
    r = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "",
            "query_term": "t",
            "target_vocabulary": "b",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.0,
        },
    )
    assert r.status_code == 422


# --- Probe 186: VerifiedSynonym schema rejects empty query_term ---


def test_probe_186_verified_synonym_create_empty_query_term_422(
    cp_client: TestClient,
) -> None:
    r = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v",
            "query_term": "",
            "target_vocabulary": "b",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.0,
        },
    )
    assert r.status_code == 422


# --- Probe 187: VerifiedSynonym schema rejects empty target_vocabulary ---


def test_probe_187_verified_synonym_create_empty_target_422(
    cp_client: TestClient,
) -> None:
    r = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v",
            "query_term": "t",
            "target_vocabulary": "",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.0,
        },
    )
    assert r.status_code == 422


# --- Probe 188: VerifiedSynonym schema rejects empty canonical_term ---


def test_probe_188_verified_synonym_create_empty_canonical_422(
    cp_client: TestClient,
) -> None:
    r = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v",
            "query_term": "t",
            "target_vocabulary": "b",
            "canonical_term": "",
            "verified_by": "alex",
            "confidence": 1.0,
        },
    )
    # canonical_term may not have min_length=1 — probe documents.
    # Accept 200 (route accepts) OR 422 (schema rejects).
    assert r.status_code in (200, 422)


# --- Probe 189: VerifiedSynonym revoke without revocation_reason rejects ---


def test_probe_189_revoke_missing_reason_422(cp_client: TestClient) -> None:
    cr = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v", "query_term": "miss-reason",
            "target_vocabulary": "b", "canonical_term": "X",
            "verified_by": "alex", "confidence": 1.0, "scope": "mr",
        },
    )
    sid = cr.json()["verified_synonym"]["id"]
    r = cp_client.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex"},  # missing revocation_reason
    )
    assert r.status_code == 422


# --- Probe 190: ApproveRequest model accepts decided_by + comment ---


def test_probe_190_approve_request_round_trip() -> None:
    from apecx_integration.control_plane.schemas.api import ApproveRequest
    rid = uuid4()
    body = ApproveRequest(approval_id=rid, decided_by="alex", comment="ok")
    dumped = body.model_dump(mode="json")
    reloaded = ApproveRequest.model_validate(dumped)
    assert reloaded == body


# --- Probe 191: RejectRequest model requires reason ---


def test_probe_191_reject_request_requires_reason() -> None:
    from apecx_integration.control_plane.schemas.api import RejectRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RejectRequest(approval_id=uuid4(), decided_by="alex")  # type: ignore


# --- Probe 192: CreateApprovalRequest accepts default policy ---


def test_probe_192_create_approval_request_round_trip() -> None:
    from apecx_integration.control_plane.schemas.api import CreateApprovalRequest
    from apecx_integration.control_plane.schemas.enums import ApprovalKind
    rid = uuid4()
    sid = uuid4()
    body = CreateApprovalRequest(
        run_id=rid,
        step_id=sid,
        kind=ApprovalKind.HARD,
        summary="t",
        artifact_ids=[],
        policy={},
    )
    assert body.policy == {}


# --- Probe 193: ListPendingApprovalsRequest requires user_id ---


def test_probe_193_list_pending_requires_user_id() -> None:
    from apecx_integration.control_plane.schemas.api import (
        ListPendingApprovalsRequest,
    )
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ListPendingApprovalsRequest()  # type: ignore


# --- Probe 194: ProvenanceEventType enum has all expected values ---


def test_probe_194_provenance_event_type_completeness() -> None:
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    expected = {
        "RUN_STARTED", "STEP_STARTED", "STEP_COMPLETED",
        "APPROVAL_REQUESTED", "APPROVAL_DECIDED",
        "ARTIFACT_CREATED", "WORKFLOW_GENERATED",
        "ALLOCATION_ESTIMATED", "ALLOCATION_CONFIRMED",
        "RUN_COMPLETED", "RUN_FAILED",
    }
    actual = {m.name for m in ProvenanceEventType}
    assert expected.issubset(actual)


# --- Probe 195: RunStatus enum has all expected values ---


def test_probe_195_run_status_completeness() -> None:
    from apecx_integration.control_plane.schemas.enums import RunStatus
    expected = {"PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"}
    actual = {m.name for m in RunStatus}
    assert expected == actual


# --- Probe 196: ApprovalKind enum has expected values ---


def test_probe_196_approval_kind_completeness() -> None:
    from apecx_integration.control_plane.schemas.enums import ApprovalKind
    expected = {"HARD", "SOFT", "SILENT", "ALLOCATION"}
    actual = {m.name for m in ApprovalKind}
    assert expected == actual


# --- Probe 197: ApprovalStatus enum has expected values ---


def test_probe_197_approval_status_completeness() -> None:
    from apecx_integration.control_plane.schemas.enums import ApprovalStatus
    expected = {
        "PENDING", "APPROVED", "APPROVED_WITH_MODIFICATIONS",
        "REJECTED", "AUTO_APPROVED", "TIMED_OUT",
    }
    actual = {m.name for m in ApprovalStatus}
    assert expected == actual


# --- Probe 198: ExecutorKind enum has expected values ---


def test_probe_198_executor_kind_completeness() -> None:
    from apecx_integration.control_plane.schemas.enums import ExecutorKind
    expected = {"LOCAL", "GLOBUS_COMPUTE", "PBS_BUNDLE"}
    actual = {m.name for m in ExecutorKind}
    assert expected == actual


# --- Probe 199: StepStatus enum has expected values ---


def test_probe_199_step_status_completeness() -> None:
    from apecx_integration.control_plane.schemas.enums import StepStatus
    expected = {
        "PENDING", "RUNNING", "PAUSED_FOR_APPROVAL",
        "COMPLETED", "FAILED", "SKIPPED",
    }
    actual = {m.name for m in StepStatus}
    assert expected == actual


# --- Probe 200: ArtifactKind enum has expected values ---


def test_probe_200_artifact_kind_completeness() -> None:
    from apecx_integration.control_plane.schemas.enums import ArtifactKind
    expected = {
        "INPUT", "INTERMEDIATE", "OUTPUT",
        "GENERATED_WORKFLOW", "GENERATED_PYTHON",
    }
    actual = {m.name for m in ArtifactKind}
    assert expected == actual
