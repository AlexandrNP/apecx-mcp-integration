"""Unit tests for the Control Plane ORM models (T09).

These tests verify that:
1. The declarative metadata is consistent (DDL generation succeeds).
2. Each ORM model round-trips through a real SQLite database.
3. Foreign keys and relationships behave.

No mocks needed — an in-memory SQLite is cheap enough to use for unit tests,
and it exercises the real SQLAlchemy machinery. The parity rule (workspace
CLAUDE.md 2026-04-21) does not apply here because there is no mock.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from apecx_integration.control_plane.models import (
    AllocationEstimate,
    Approval,
    Artifact,
    Base,
    Component,
    GeneratedArtifact,
    ProvenanceEvent,
    Run,
    Step,
    VerifiedSynonym,
)
from apecx_integration.control_plane.schemas.enums import (
    ApprovalKind,
    ApprovalStatus,
    ArtifactKind,
    ComponentTestStatus,
    ExecutorKind,
    ProvenanceEventType,
    RunStatus,
    StepStatus,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


@pytest.fixture(name="session")
def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _hash() -> str:
    return "a" * 64


def test_metadata_creates_all_tables_without_error() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    table_names = set(Base.metadata.tables.keys())
    assert {
        "run",
        "step",
        "approval",
        "artifact",
        "generated_artifact",
        "provenance_event",
        "component",
        "allocation_estimate",
        "verified_synonym",
    }.issubset(table_names)


def test_run_and_step_relationship(session: Session) -> None:
    run = Run(user_id="alex", created_at=_now())
    Step(run=run, step_name="entity_extraction")
    session.add(run)
    session.commit()
    session.refresh(run)
    assert len(run.steps) == 1
    assert run.steps[0].status is StepStatus.PENDING
    assert run.steps[0].executor is ExecutorKind.LOCAL


def test_approval_cascades_with_step(session: Session) -> None:
    run = Run(user_id="alex", created_at=_now())
    step = Step(run=run, step_name="gate")
    approval = Approval(step=step, kind=ApprovalKind.HARD, created_at=_now())
    session.add(run)
    session.commit()

    assert approval.status is ApprovalStatus.PENDING
    session.delete(step)
    session.commit()
    remaining = session.scalars(select(Approval)).all()
    assert remaining == []


def test_artifact_and_generated_artifact(session: Session) -> None:
    run = Run(user_id="alex", created_at=_now())
    session.add(run)
    session.flush()
    art = Artifact(
        run_id=run.id,
        kind=ArtifactKind.GENERATED_WORKFLOW,
        location="artifacts/w1.yml",
        content_hash=_hash(),
        size_bytes=1234,
        mime_type="text/yaml",
        created_at=_now(),
    )
    session.add(art)
    session.flush()
    gen = GeneratedArtifact(
        artifact_id=art.id,
        source_prompt="Run VIOLIN × BV-BRC",
        library_version="0.1.0",
        llm_model="claude-opus-4-7",
        llm_model_version_hash=_hash(),
    )
    session.add(gen)
    session.commit()
    assert session.get(GeneratedArtifact, art.id) is not None


def test_provenance_event_round_trip(session: Session) -> None:
    run = Run(user_id="alex", created_at=_now())
    session.add(run)
    session.flush()  # populate run.id before we reference it
    event = ProvenanceEvent(
        run_id=run.id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="system",
        timestamp=_now(),
        event_hash=_hash(),
    )
    session.add(event)
    session.commit()
    loaded = session.scalars(select(ProvenanceEvent)).one()
    assert loaded.event_type is ProvenanceEventType.RUN_STARTED
    assert loaded.prev_event_hash is None


def test_component_primary_key_is_string_id(session: Session) -> None:
    comp = Component(
        id="io.violin_gene_reader",
        name="VIOLIN gene reader",
        version="0.1.0",
        description="Reads Gene_Information.csv",
        implementation_path="apecx_integration/steps/violin/gene_reader.py",
    )
    session.add(comp)
    session.commit()
    loaded = session.get(Component, "io.violin_gene_reader")
    assert loaded is not None
    assert loaded.test_status is ComponentTestStatus.UNTESTED


def test_allocation_estimate(session: Session) -> None:
    run = Run(user_id="alex", created_at=_now())
    session.add(run)
    session.flush()
    est = AllocationEstimate(
        run_id=run.id,
        estimated_core_hours=42.0,
        estimated_wall_time_seconds=3600.0,
        endpoint="polaris",
        created_at=_now(),
    )
    session.add(est)
    session.commit()
    loaded = session.scalars(select(AllocationEstimate)).one()
    assert loaded.estimated_core_hours == 42.0
    assert loaded.user_confirmed is False


def test_verified_synonym_active_defaults(session: Session) -> None:
    syn = VerifiedSynonym(
        source_vocabulary="user_query",
        query_term="chikungunya",
        target_vocabulary="violin.pathogen_name",
        canonical_term="Chikungunya virus",
        verified_by="alex",
        verified_at=_now(),
        confidence=0.98,
    )
    session.add(syn)
    session.commit()
    loaded = session.scalars(select(VerifiedSynonym)).one()
    assert loaded.is_active is True
    assert loaded.revoked_by is None


def test_verified_synonym_revocation_fields(session: Session) -> None:
    revoked = VerifiedSynonym(
        source_vocabulary="user_query",
        query_term="chik",
        target_vocabulary="violin.pathogen_name",
        canonical_term="Chikungunya virus",
        verified_by="alex",
        verified_at=_now(),
        confidence=0.72,
        is_active=False,
        revoked_by="alex",
        revoked_at=_now(),
        revocation_reason="abbreviation too ambiguous",
    )
    session.add(revoked)
    session.commit()
    loaded = session.scalars(select(VerifiedSynonym)).one()
    assert loaded.is_active is False
    assert loaded.revocation_reason == "abbreviation too ambiguous"


def test_run_default_status_is_pending(session: Session) -> None:
    run = Run(user_id="alex", created_at=_now())
    session.add(run)
    session.commit()
    loaded = session.scalars(select(Run)).one()
    assert loaded.status is RunStatus.PENDING
