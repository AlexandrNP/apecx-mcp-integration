"""Pydantic entity schemas mirroring architectural_plan.md §4.

These are the shared shapes between the HTTP API (TX1) and the SQLAlchemy models
(T09). The SQLAlchemy layer will reference these for validation and for OpenAPI
schema generation via FastAPI.

Round 3 note: ExecutorKind defaults to LOCAL per the local-default execution
constraint. HPC-export kinds (globus_compute, pbs_bundle) are opt-in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

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


class _EntityBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Run(_EntityBase):
    id: UUID = Field(default_factory=uuid4)
    user_id: str
    workflow_config_id: UUID | None = None
    status: RunStatus = RunStatus.PENDING
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    parent_run_id: UUID | None = None


class Step(_EntityBase):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    step_name: str
    executor: ExecutorKind = ExecutorKind.LOCAL
    status: StepStatus = StepStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    log_location: str | None = None


class Approval(_EntityBase):
    id: UUID = Field(default_factory=uuid4)
    step_id: UUID
    kind: ApprovalKind
    status: ApprovalStatus = ApprovalStatus.PENDING
    policy: dict[str, Any] = Field(default_factory=dict)
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class Artifact(_EntityBase):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    step_id: UUID | None = None
    kind: ArtifactKind
    location: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    mime_type: str
    created_at: datetime


class GeneratedArtifact(_EntityBase):
    """Subtype of Artifact with LLM-generation provenance.

    See architectural_plan.md §4 (GeneratedArtifact).
    """

    artifact_id: UUID
    source_prompt: str
    library_version: str
    llm_model: str
    llm_model_version_hash: str
    composition_summary: dict[str, Any] = Field(default_factory=dict)
    parent_artifact_id: UUID | None = None


class ProvenanceEvent(_EntityBase):
    """Hash-chained, append-only provenance record.

    See architectural_plan.md §4.1.
    """

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    event_type: ProvenanceEventType
    actor: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_event_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Component(_EntityBase):
    """A library component available for composition.

    Mirrors architectural_plan.md §4 Component table. The embedding vector is
    stored separately in the RAG index (T03), not on this record.

    Naming: ``io_schema`` holds the AP §4 "schema" field (json-schema for inputs
    and outputs). We avoid the literal name ``schema`` because it shadows a
    Pydantic BaseModel attribute.
    """

    id: str
    name: str
    version: str
    description: str
    io_schema: dict[str, Any] = Field(default_factory=dict)
    implementation_path: str
    test_status: ComponentTestStatus = ComponentTestStatus.UNTESTED
    domain: str = "generic"
    examples: list[str] = Field(default_factory=list)


class VerifiedSynonym(_EntityBase):
    """A user-verified synonym mapping persisted across runs.

    Round 3 addition (user directive 2026-04-21): instead of asking a human to
    approve the same synonym on every run, we remember past approvals and only
    surface novel terms to the HITL gate.

    Naming conventions:
    - ``source_vocabulary``: which corpus the query term came from, e.g.
      ``user_query``, ``violin.vaccine_name``, ``bvbrc.strain_name``.
    - ``target_vocabulary``: where the canonical term lives, e.g.
      ``violin.pathogen_id``, ``bvbrc.genome_id``.
    - ``canonical_term``: the resolved identifier (may be an ID or a name
       string, depending on the target vocabulary).
    - ``scope``: optional narrowing, e.g. restrict a mapping to a specific
      taxonomic family or dataset version.

    The same query term may have multiple verified mappings with different
    ``target_vocabulary`` values (one per corpus). Uniqueness is intended at
    the ``(source_vocabulary, query_term, target_vocabulary, scope)`` tuple
    level; the T09 migration will enforce that.
    """

    id: UUID = Field(default_factory=uuid4)
    source_vocabulary: str
    query_term: str
    target_vocabulary: str
    canonical_term: str
    scope: str | None = None
    verified_by: str
    verified_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    source_run_id: UUID | None = None
    comment: str | None = None


class AllocationEstimate(_EntityBase):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    estimated_core_hours: float = Field(ge=0.0)
    estimated_wall_time_seconds: float = Field(ge=0.0)
    estimated_memory_gb: float | None = Field(default=None, ge=0.0)
    endpoint: str
    user_confirmed: bool = False
    user_confirmed_at: datetime | None = None
    actual_core_hours: float | None = Field(default=None, ge=0.0)
