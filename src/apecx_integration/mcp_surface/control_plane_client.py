"""Thin HTTP client the MCP surface uses to call the Control Plane (Tier 2).

The client is intentionally dumb: marshals envelope types to/from JSON and
raises on non-2xx. Status-code handling:

- 2xx → parse the body into the declared response model and return it.
- 501 → ``NotImplementedError`` (``detail`` as message). Only
        ``/hpc/submit`` still stubs with 501 (demoted-optional HPC
        executor work, T04/T05 runtime).
- 503 → ``ControlPlaneDependencyError`` (``detail`` as message).
        Means a composer or approval-policy dependency isn't
        configured on the Control Plane (see ``get_composer`` /
        ``get_approval_policy`` / ``get_local_executor`` in
        ``control_plane/dependencies.py``). Pre-2026-04-24 this was
        a raw ``httpx.HTTPStatusError`` — confusing for MCP callers
        because the traceback exposed httpx internals; now wrapped
        for consistency with the 501 path. Audit §3.3.
- other non-2xx → ``httpx.HTTPStatusError`` via ``raise_for_status()``.

Real-backed endpoints (as of 2026-04-22):
  /workflows/start, /workflows/plan, /workflows/diff, /workflows/execute,
  /approvals/*, /runs/*, verified-synonyms, /metrics/*,
  /hpc/estimate, /hpc/confirm, /hpc/export, /hpc/ingest.

Still stubbed 501: /hpc/submit (needs T04 Globus / T05 qsub runtime).

See architectural_plan.md §3.2 (why HTTP between Tier 1 and Tier 2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel

from apecx_integration.control_plane.schemas.api import (
    ApprovalResponse,
    ApproveRequest,
    ConfirmAllocationRequest,
    ConfirmAllocationResponse,
    CorrectRequest,
    CreateApprovalRequest,
    CreateApprovalResponse,
    CreateVerifiedSynonymRequest,
    EstimateCostRequest,
    EstimateCostResponse,
    ExecuteWorkflowRequest,
    ExecuteWorkflowResponse,
    ExportHpcBundleRequest,
    ExportHpcBundleResponse,
    GeneratePlanRequest,
    GeneratePlanResponse,
    GetArtifactRequest,
    GetArtifactResponse,
    GetStatusRequest,
    GetStatusResponse,
    IngestHpcBundleRequest,
    IngestHpcBundleResponse,
    ListPendingApprovalsRequest,
    ListPendingApprovalsResponse,
    ListRunsRequest,
    ListRunsResponse,
    RejectRequest,
    RevokeVerifiedSynonymRequest,
    ShowYamlDiffRequest,
    ShowYamlDiffResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
    VerifiedSynonymLookupRequest,
    VerifiedSynonymLookupResponse,
    VerifiedSynonymResponse,
)

R = TypeVar("R", bound=BaseModel)


class ControlPlaneDependencyError(RuntimeError):
    """Raised when the Control Plane returns 503 because a backend
    dependency (composer, approval policy, executor) isn't configured.

    Distinct from ``NotImplementedError`` (which the client raises
    on 501) so callers can differentiate "this endpoint doesn't
    exist yet" from "this endpoint exists but is misconfigured."
    """


class ControlPlaneClient:
    """HTTP client to the Control Plane.

    All composer-backed + HPC (non-submit) routes are exposed. The
    MCP surface's tools wrap these one-to-one.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def __aenter__(self) -> ControlPlaneClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(
        self,
        path: str,
        body: BaseModel,
        response_model: type[R],
    ) -> R:
        resp = await self._client.post(path, json=body.model_dump(mode="json"))
        if resp.status_code == httpx.codes.NOT_IMPLEMENTED:
            detail = self._extract_detail(resp.json())
            raise NotImplementedError(detail or "Control Plane endpoint not implemented yet")
        if resp.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            detail = self._extract_detail(resp.json())
            raise ControlPlaneDependencyError(
                detail
                or f"Control Plane returned 503 from {path} "
                "(composer / approval-policy / executor not configured)"
            )
        resp.raise_for_status()
        return response_model.model_validate(resp.json())

    @staticmethod
    def _extract_detail(payload: Mapping[str, Any]) -> str | None:
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        return None

    async def healthz(self) -> dict[str, str]:
        resp = await self._client.get("/healthz")
        resp.raise_for_status()
        return resp.json()

    async def start_workflow(self, body: StartWorkflowRequest) -> StartWorkflowResponse:
        return await self._post("/workflows/start", body, StartWorkflowResponse)

    async def generate_plan(self, body: GeneratePlanRequest) -> GeneratePlanResponse:
        return await self._post("/workflows/plan", body, GeneratePlanResponse)

    async def show_yaml_diff(self, body: ShowYamlDiffRequest) -> ShowYamlDiffResponse:
        return await self._post("/workflows/diff", body, ShowYamlDiffResponse)

    async def execute_workflow(
        self, body: ExecuteWorkflowRequest
    ) -> ExecuteWorkflowResponse:
        return await self._post("/workflows/execute", body, ExecuteWorkflowResponse)

    # ---- /hpc (T07 + T05) ---------------------------------------------

    async def estimate_cost(
        self, body: EstimateCostRequest
    ) -> EstimateCostResponse:
        return await self._post("/hpc/estimate", body, EstimateCostResponse)

    async def confirm_allocation(
        self, body: ConfirmAllocationRequest
    ) -> ConfirmAllocationResponse:
        return await self._post(
            "/hpc/confirm", body, ConfirmAllocationResponse
        )

    async def export_hpc_bundle(
        self, body: ExportHpcBundleRequest
    ) -> ExportHpcBundleResponse:
        return await self._post(
            "/hpc/export", body, ExportHpcBundleResponse
        )

    async def ingest_hpc_bundle(
        self, body: IngestHpcBundleRequest
    ) -> IngestHpcBundleResponse:
        return await self._post(
            "/hpc/ingest", body, IngestHpcBundleResponse
        )

    async def create_approval(self, body: CreateApprovalRequest) -> CreateApprovalResponse:
        return await self._post("/approvals/", body, CreateApprovalResponse)

    async def get_approval(self, approval_id: UUID) -> ApprovalResponse:
        """Poll the current state of an approval.

        Used by the nanobrain ApprovalStep (T10) while it's paused.
        2xx returns the Approval with current status; 404 if unknown.
        """
        resp = await self._client.get(f"/approvals/{approval_id}")
        resp.raise_for_status()
        return ApprovalResponse.model_validate(resp.json())

    async def approve(self, body: ApproveRequest) -> ApprovalResponse:
        return await self._post("/approvals/approve", body, ApprovalResponse)

    async def reject(self, body: RejectRequest) -> ApprovalResponse:
        return await self._post("/approvals/reject", body, ApprovalResponse)

    async def correct(self, body: CorrectRequest) -> ApprovalResponse:
        return await self._post("/approvals/correct", body, ApprovalResponse)

    async def list_pending_approvals(
        self, body: ListPendingApprovalsRequest
    ) -> ListPendingApprovalsResponse:
        return await self._post("/approvals/pending", body, ListPendingApprovalsResponse)

    async def list_runs(self, body: ListRunsRequest) -> ListRunsResponse:
        return await self._post("/runs/list", body, ListRunsResponse)

    async def get_status(self, body: GetStatusRequest) -> GetStatusResponse:
        return await self._post("/runs/status", body, GetStatusResponse)

    async def get_artifact(self, body: GetArtifactRequest) -> GetArtifactResponse:
        return await self._post("/runs/artifact", body, GetArtifactResponse)

    # ---- /verified_synonyms (T02) -------------------------------------

    async def lookup_verified_synonyms(
        self, body: VerifiedSynonymLookupRequest
    ) -> VerifiedSynonymLookupResponse:
        return await self._post("/verified_synonyms/lookup", body, VerifiedSynonymLookupResponse)

    async def create_verified_synonym(
        self, body: CreateVerifiedSynonymRequest
    ) -> VerifiedSynonymResponse:
        return await self._post("/verified_synonyms/", body, VerifiedSynonymResponse)

    async def revoke_verified_synonym(
        self, synonym_id: UUID, body: RevokeVerifiedSynonymRequest
    ) -> VerifiedSynonymResponse:
        resp = await self._client.patch(
            f"/verified_synonyms/{synonym_id}",
            json=body.model_dump(mode="json"),
        )
        resp.raise_for_status()
        return VerifiedSynonymResponse.model_validate(resp.json())
