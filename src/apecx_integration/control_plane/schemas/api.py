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


class RejectRequest(_APIBase):
    approval_id: UUID
    reason: str = Field(min_length=1)


class CorrectRequest(_APIBase):
    approval_id: UUID
    modifications: dict[str, object]


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
