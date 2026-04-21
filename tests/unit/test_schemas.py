"""Unit tests for Control Plane schemas (TX1 foundation).

These tests verify that the Pydantic models from architectural_plan.md §4
serialize round-trip, enforce their constraints, and expose expected defaults.
No mocks needed; this is pure schema validation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apecx_integration.control_plane.schemas import (
    AllocationEstimate,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    Component,
    ComponentTestStatus,
    ExecutorKind,
    GeneratedArtifact,
    ProvenanceEvent,
    ProvenanceEventType,
    Run,
    RunStatus,
    Step,
    StepStatus,
    VerifiedSynonym,
)
from apecx_integration.control_plane.schemas.api import (
    ApproveRequest,
    EstimateCostResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _valid_hash() -> str:
    return "0" * 64


def test_run_defaults() -> None:
    run = Run(user_id="alex", created_at=_now())
    assert run.status is RunStatus.PENDING
    assert run.started_at is None
    assert run.completed_at is None
    assert isinstance(run.id, type(uuid4()))


def test_step_defaults_local_executor() -> None:
    """Round 3: local executor is the default."""
    step = Step(run_id=uuid4(), step_name="read_snapshot")
    assert step.executor is ExecutorKind.LOCAL
    assert step.status is StepStatus.PENDING
    assert step.input_artifact_ids == []


def test_approval_lifecycle_fields_start_empty() -> None:
    approval = Approval(step_id=uuid4(), kind=ApprovalKind.SOFT)
    assert approval.status is ApprovalStatus.PENDING
    assert approval.decided_by is None
    assert approval.decided_at is None


def test_artifact_hash_must_be_sha256() -> None:
    with pytest.raises(ValidationError):
        Artifact(
            run_id=uuid4(),
            kind=ArtifactKind.OUTPUT,
            location="/tmp/x",
            content_hash="not-a-hash",
            size_bytes=0,
            mime_type="text/plain",
            created_at=_now(),
        )


def test_artifact_size_must_be_nonnegative() -> None:
    with pytest.raises(ValidationError):
        Artifact(
            run_id=uuid4(),
            kind=ArtifactKind.OUTPUT,
            location="/tmp/x",
            content_hash=_valid_hash(),
            size_bytes=-1,
            mime_type="text/plain",
            created_at=_now(),
        )


def test_generated_artifact_round_trip() -> None:
    a = GeneratedArtifact(
        artifact_id=uuid4(),
        source_prompt="test",
        library_version="0.1.0",
        llm_model="claude-opus-4-7",
        llm_model_version_hash=_valid_hash(),
    )
    reloaded = GeneratedArtifact.model_validate_json(a.model_dump_json())
    assert reloaded == a


def test_provenance_event_requires_event_hash() -> None:
    with pytest.raises(ValidationError):
        ProvenanceEvent(
            run_id=uuid4(),
            event_type=ProvenanceEventType.RUN_STARTED,
            actor="system",
            timestamp=_now(),
        )


def test_provenance_event_prev_hash_optional() -> None:
    event = ProvenanceEvent(
        run_id=uuid4(),
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="system",
        timestamp=_now(),
        event_hash=_valid_hash(),
    )
    assert event.prev_event_hash is None


def test_component_defaults() -> None:
    c = Component(
        id="io.violin_gene_reader",
        name="VIOLIN gene reader",
        version="0.1.0",
        description="Reads Gene_Information.csv from the VIOLIN snapshot.",
        implementation_path="apecx_integration/steps/violin/gene_reader.py",
    )
    assert c.test_status is ComponentTestStatus.UNTESTED
    assert c.domain == "generic"
    assert c.examples == []


def test_allocation_estimate_rejects_negative_core_hours() -> None:
    with pytest.raises(ValidationError):
        AllocationEstimate(
            run_id=uuid4(),
            estimated_core_hours=-1.0,
            estimated_wall_time_seconds=60.0,
            endpoint="polaris",
        )


def test_start_workflow_request_rejects_empty_description() -> None:
    with pytest.raises(ValidationError):
        StartWorkflowRequest(description="", user_id="alex")


def test_start_workflow_request_round_trip() -> None:
    req = StartWorkflowRequest(
        description="Run VIOLIN × BV-BRC on alphaviruses",
        user_id="alex",
    )
    assert req.preferred_executor is ExecutorKind.LOCAL
    reloaded = StartWorkflowRequest.model_validate_json(req.model_dump_json())
    assert reloaded == req


def test_start_workflow_response_requires_run_and_artifact() -> None:
    run = Run(user_id="alex", created_at=_now())
    resp = StartWorkflowResponse(run=run, generated_workflow_artifact_id=uuid4())
    assert resp.run == run


def test_approve_request_accepts_empty_comment() -> None:
    req = ApproveRequest(approval_id=uuid4())
    assert req.comment == ""


def test_estimate_cost_response_confidence_interval_is_tuple() -> None:
    resp = EstimateCostResponse(
        total_core_hours=100.0,
        per_step_core_hours={"s1": 50.0, "s2": 50.0},
        confidence_interval=(50.0, 150.0),
        endpoint="polaris",
    )
    assert resp.confidence_interval == (50.0, 150.0)


def test_verified_synonym_defaults_and_round_trip() -> None:
    syn = VerifiedSynonym(
        source_vocabulary="user_query",
        query_term="chikungunya",
        target_vocabulary="violin.pathogen_name",
        canonical_term="Chikungunya virus",
        verified_by="alex",
        verified_at=_now(),
        confidence=0.98,
    )
    assert syn.scope is None
    assert syn.source_run_id is None
    reloaded = VerifiedSynonym.model_validate_json(syn.model_dump_json())
    assert reloaded == syn


def test_verified_synonym_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValidationError):
        VerifiedSynonym(
            source_vocabulary="user_query",
            query_term="x",
            target_vocabulary="violin.pathogen_name",
            canonical_term="y",
            verified_by="alex",
            verified_at=_now(),
            confidence=1.5,
        )
    with pytest.raises(ValidationError):
        VerifiedSynonym(
            source_vocabulary="user_query",
            query_term="x",
            target_vocabulary="violin.pathogen_name",
            canonical_term="y",
            verified_by="alex",
            verified_at=_now(),
            confidence=-0.1,
        )


def test_extra_fields_are_rejected() -> None:
    """We use extra='forbid' so that typo'd field names fail loudly."""

    with pytest.raises(ValidationError):
        Run.model_validate(
            {"user_id": "alex", "created_at": _now(), "userid_typo": "alex"}
        )
