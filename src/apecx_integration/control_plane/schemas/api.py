"""Request / response envelopes for the Tier 1 ↔ Tier 2 API contract (TX1).

One envelope per MCP tool in architectural_plan.md §3. These are the shapes the
MCP surface (Tier 1) POSTs to the FastAPI app (Tier 2). FastAPI generates the
OpenAPI spec from these, which doubles as the MCP tool schema (AP §3.2).

Round 3 note: HPC-related tools (estimate_cost, submit_hpc, export_hpc_bundle)
are defined here but guarded at the route level — they raise HTTP 501 if the
control plane was not started with HPC support enabled.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apecx_integration.control_plane.schemas.entities import (
    Approval,
    Artifact,
    Run,
    Step,
)
from apecx_integration.control_plane.schemas.enums import (
    ApprovalKind,
    ExecutorKind,
    RunStatus,
    StepCategory,
)


class _APIBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- start_workflow ---------------------------------------------------------


class StartWorkflowRequest(_APIBase):
    description: str = Field(min_length=1)
    user_id: str
    preferred_executor: ExecutorKind = ExecutorKind.LOCAL


class StartWorkflowResponse(_APIBase):
    run: Run
    generated_workflow_artifact_id: UUID


# --- generate_plan ---------------------------------------------------------


class GeneratePlanRequest(_APIBase):
    description: str = Field(min_length=1)
    library_version: str | None = None


class StepPlan(_APIBase):
    step_id: str
    step_name: str
    category: StepCategory
    reference_component_id: str | None = None
    rationale: str


class GeneratePlanResponse(_APIBase):
    plan: list[StepPlan]
    yaml_text: str
    generated_artifact_id: UUID


# --- show_yaml_diff / show_novel_python / show_diff_summary -----------------


class ShowYamlDiffRequest(_APIBase):
    run_id: UUID


class ShowYamlDiffResponse(_APIBase):
    yaml_text: str
    novel_python_by_step: dict[str, str] = Field(default_factory=dict)
    categorization: list[StepPlan]
    summary_sentence: str


# --- approve / reject / correct --------------------------------------------


class ApproveRequest(_APIBase):
    approval_id: UUID
    comment: str = ""
    # TX1: no auth layer yet; the MCP client (or whoever calls this)
    # can attribute the decision. Defaults to "api_user" when unset so
    # early integrations don't have to supply it. When auth lands this
    # becomes derived from the session/token.
    decided_by: str = "api_user"


class RejectRequest(_APIBase):
    approval_id: UUID
    reason: str = Field(min_length=1)
    decided_by: str = "api_user"


class CorrectRequest(_APIBase):
    approval_id: UUID
    modifications: dict[str, object]
    decided_by: str = "api_user"


class ApprovalResponse(_APIBase):
    approval: Approval


# --- list_pending_approvals / list_runs / get_status -----------------------


class ListPendingApprovalsRequest(_APIBase):
    user_id: str


class ListPendingApprovalsResponse(_APIBase):
    approvals: list[Approval]


class ListRunsRequest(_APIBase):
    user_id: str
    status_filter: RunStatus | None = None
    limit: int = Field(default=50, ge=1, le=500)


class ListRunsResponse(_APIBase):
    runs: list[Run]


class GetStatusRequest(_APIBase):
    run_id: UUID


class GetStatusResponse(_APIBase):
    run: Run
    steps: list[Step]
    pending_approval: Approval | None = None


# --- get_artifact ----------------------------------------------------------


class GetArtifactRequest(_APIBase):
    artifact_id: UUID


class GetArtifactResponse(_APIBase):
    artifact: Artifact
    inline_bytes: bytes | None = None
    reason_inline_omitted: str | None = None


# --- estimate_cost / submit_hpc / export_hpc_bundle (optional) --------------


class EstimateCostRequest(_APIBase):
    run_id: UUID


class EstimateCostResponse(_APIBase):
    total_core_hours: float = Field(ge=0.0)
    per_step_core_hours: dict[str, float]
    confidence_interval: tuple[float, float]
    endpoint: str
    novel_python_capped_at: float | None = None


class ConfirmAllocationRequest(_APIBase):
    run_id: UUID
    confirmed_core_hours: float = Field(ge=0.0)


class ConfirmAllocationResponse(_APIBase):
    run_id: UUID
    confirmed: bool


class SubmitHpcRequest(_APIBase):
    run_id: UUID
    executor: ExecutorKind
    allocation_account: str | None = None


class SubmitHpcResponse(_APIBase):
    run_id: UUID
    submitted_executor: ExecutorKind
    external_job_id: str | None = None


class ExportHpcBundleRequest(_APIBase):
    run_id: UUID
    target_system: str
    output_directory: str


class ExportHpcBundleResponse(_APIBase):
    bundle_path: str
    submit_command: str


# --- create_approval (internal, called from ApprovalStep) -------------------


class CreateApprovalRequest(_APIBase):
    run_id: UUID
    step_id: UUID
    kind: ApprovalKind
    summary: str
    artifact_ids: list[UUID] = Field(default_factory=list)
    policy: dict[str, object] = Field(default_factory=dict)


class CreateApprovalResponse(_APIBase):
    approval: Approval


# --- /metrics/approvals (TX3 review-UX telemetry) --------------------------


class ApprovalMetricsResponse(_APIBase):
    """Aggregate telemetry over approval decisions in a time window.

    Rubber-stamping detection (AP §7 risk #4): if median time-to-decide
    drops below 5 seconds across a week with N>5 approvals, the reviewer
    is likely not reading — ``rubber_stamping_suspected`` becomes true.
    Document the threshold so it's checked at retros (TX3 AC4).
    """

    count: int = Field(
        ge=0, description="Approvals with both started and decided timestamps in the window."
    )
    median_time_to_decide_seconds: float | None = Field(
        description="Median seconds between APPROVAL_REQUESTED and APPROVAL_DECIDED. Null when count==0.",
    )
    p95_time_to_decide_seconds: float | None = Field(
        description="95th-percentile time-to-decide. Null when count < 20 (statistic is meaningless below that sample size).",
    )
    percent_auto_approved: float = Field(
        ge=0.0,
        le=100.0,
        description="Share of decisions with final status AUTO_APPROVED. Always 0 when count==0.",
    )
    percent_rejected: float = Field(
        ge=0.0,
        le=100.0,
        description="Share of decisions with final status REJECTED. Always 0 when count==0.",
    )
    rubber_stamping_suspected: bool = Field(
        description="True when count>5 and median_time_to_decide_seconds<5.",
    )
    window_start_iso: str = Field(description="Caller-supplied `since` timestamp, echoed.")


# --- /verified_synonyms (T02: batched cache lookup + create + revoke) -----


class VerifiedSynonymLookupRequest(_APIBase):
    """Batched cache lookup. Every workflow run starts with a lookup
    for the entity terms it extracted from the user query; this is
    the hot path for HARD-synonym strategy, so the API is batched.
    """

    source_vocabulary: str = Field(min_length=1)
    target_vocabulary: str = Field(min_length=1)
    query_terms: list[str] = Field(min_length=1, max_length=500)
    scope: str | None = None


from apecx_integration.control_plane.schemas.entities import VerifiedSynonym  # noqa: E402


class VerifiedSynonymMatch(_APIBase):
    """One cache-lookup result. ``result`` is the active
    :class:`VerifiedSynonym` row for the term, or null when the term
    is novel (no human-approved mapping yet).
    """

    query_term: str
    result: VerifiedSynonym | None = None


class VerifiedSynonymLookupResponse(_APIBase):
    matches: list[VerifiedSynonymMatch]


class CreateVerifiedSynonymRequest(_APIBase):
    """Write-back after ApprovalStep approves a novel mapping.

    ``verified_by`` is the reviewer's identifier (same placeholder shape
    as ApprovalStep's ``decided_by``: defaults to ``"api_user"`` until
    auth lands).
    """

    source_vocabulary: str = Field(min_length=1)
    query_term: str = Field(min_length=1)
    target_vocabulary: str = Field(min_length=1)
    canonical_term: str = Field(min_length=1)
    scope: str | None = None
    verified_by: str = "api_user"
    confidence: float = Field(ge=0.0, le=1.0)
    source_run_id: UUID | None = None
    comment: str | None = None


class VerifiedSynonymResponse(_APIBase):
    verified_synonym: VerifiedSynonym


class RevokeVerifiedSynonymRequest(_APIBase):
    """Soft-delete a previously-approved mapping. Row is preserved for
    audit; ``is_active`` flips to false and the revocation fields are
    populated. Optional ``superseded_by`` points at a replacement row.
    """

    revoked_by: str = "api_user"
    revocation_reason: str = Field(min_length=1)
    superseded_by: UUID | None = None
