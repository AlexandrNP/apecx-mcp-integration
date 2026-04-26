"""SQLAlchemy 2.0 ORM models mirroring schemas/entities.py (T09).

Field-for-field mirrors of the Pydantic entities plus the DB-layer concerns:
foreign keys with explicit ondelete policy, primary-key indexing, cross-db
portable types, and soft-delete constraints where applicable.

Design notes:
- Enum columns use ``Enum(..., native_enum=False, length=N)``. ``native_enum=False``
  makes the column a portable VARCHAR(N) that still coerces to/from the Python
  enum on load. SQLite doesn't have a native enum; this lets the same schema
  work on SQLite and Postgres.
- JSON columns (lists, dicts) use explicit ``mapped_column(JSON, default=...)``
  rather than SQLAlchemy's annotation-map inference, which is finicky for
  parametrized generics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SQLAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apecx_integration.control_plane.models.base import Base
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


def _enum_col(py_enum: type, length: int = 48):
    """Shorthand for a portable enum-as-VARCHAR column."""
    return SQLAEnum(py_enum, native_enum=False, length=length, validate_strings=True)


class Run(Base):
    __tablename__ = "run"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(String(255))
    workflow_config_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifact.id", use_alter=True, name="fk_run_workflow_config"),
        nullable=True,
    )
    status: Mapped[RunStatus] = mapped_column(_enum_col(RunStatus), default=RunStatus.PENDING)
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    parent_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("run.id"), nullable=True)

    steps: Mapped[list[Step]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Step(Base):
    __tablename__ = "step"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run.id"), index=True)
    step_name: Mapped[str] = mapped_column(String(255))
    executor: Mapped[ExecutorKind] = mapped_column(
        _enum_col(ExecutorKind), default=ExecutorKind.LOCAL
    )
    status: Mapped[StepStatus] = mapped_column(_enum_col(StepStatus), default=StepStatus.PENDING)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    input_artifact_ids: Mapped[list[UUID]] = mapped_column(JSON, default=list)
    output_artifact_ids: Mapped[list[UUID]] = mapped_column(JSON, default=list)
    log_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Migration 0006: ordering key for /runs/status. ``id`` is a
    # random uuid4, so PENDING steps (started_at = NULL) returned
    # in arbitrary order before. Cluster AH (2026-04-26).
    created_at: Mapped[datetime]

    run: Mapped[Run] = relationship(back_populates="steps")
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="step", cascade="all, delete-orphan"
    )


class Approval(Base):
    __tablename__ = "approval"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    step_id: Mapped[UUID] = mapped_column(ForeignKey("step.id"), index=True)
    kind: Mapped[ApprovalKind] = mapped_column(_enum_col(ApprovalKind))
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum_col(ApprovalStatus), default=ApprovalStatus.PENDING, index=True
    )
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Migration 0005: real ordering key for /approvals/pending. ``id``
    # is a random uuid4, so ORDER BY id scrambles the operator's
    # backlog (cluster AE, 2026-04-26).
    created_at: Mapped[datetime]

    step: Mapped[Step] = relationship(back_populates="approvals")


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run.id"), index=True)
    step_id: Mapped[UUID | None] = mapped_column(ForeignKey("step.id"), nullable=True)
    kind: Mapped[ArtifactKind] = mapped_column(_enum_col(ArtifactKind))
    location: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(127))
    created_at: Mapped[datetime]


class GeneratedArtifact(Base):
    __tablename__ = "generated_artifact"

    artifact_id: Mapped[UUID] = mapped_column(ForeignKey("artifact.id"), primary_key=True)
    source_prompt: Mapped[str] = mapped_column(Text)
    library_version: Mapped[str] = mapped_column(String(64))
    llm_model: Mapped[str] = mapped_column(String(128))
    llm_model_version_hash: Mapped[str] = mapped_column(String(64))
    composition_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parent_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("artifact.id"), nullable=True
    )


class ProvenanceEvent(Base):
    __tablename__ = "provenance_event"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run.id"), index=True)
    event_type: Mapped[ProvenanceEventType] = mapped_column(_enum_col(ProvenanceEventType))
    actor: Mapped[str] = mapped_column(String(255))
    timestamp: Mapped[datetime]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), index=True)


class Component(Base):
    __tablename__ = "component"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    io_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    implementation_path: Mapped[str] = mapped_column(Text)
    test_status: Mapped[ComponentTestStatus] = mapped_column(
        _enum_col(ComponentTestStatus), default=ComponentTestStatus.UNTESTED
    )
    domain: Mapped[str] = mapped_column(String(64), default="generic")
    examples: Mapped[list[str]] = mapped_column(JSON, default=list)


class AllocationEstimate(Base):
    __tablename__ = "allocation_estimate"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run.id"), index=True)
    estimated_core_hours: Mapped[float] = mapped_column(Float)
    estimated_wall_time_seconds: Mapped[float] = mapped_column(Float)
    estimated_memory_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    endpoint: Mapped[str] = mapped_column(String(128))
    user_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    user_confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    actual_core_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Migration 0004: real ordering key for /hpc/confirm. ``id`` is a
    # random uuid4, so ``ORDER BY id DESC`` picked the wrong row when
    # UUID lex order disagreed with insertion order. See cluster AC.
    created_at: Mapped[datetime]


class VerifiedSynonym(Base):
    __tablename__ = "verified_synonym"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_vocabulary: Mapped[str] = mapped_column(String(128), index=True)
    query_term: Mapped[str] = mapped_column(String(512), index=True)
    target_vocabulary: Mapped[str] = mapped_column(String(128), index=True)
    canonical_term: Mapped[str] = mapped_column(String(512))
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_by: Mapped[str] = mapped_column(String(255))
    verified_at: Mapped[datetime]
    confidence: Mapped[float] = mapped_column(Float)
    source_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("run.id"), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("verified_synonym.id"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "source_vocabulary",
            "query_term",
            "target_vocabulary",
            "scope",
            "is_active",
            name="uq_verified_synonym_tuple",
        ),
    )
