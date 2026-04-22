"""Thin HTTP client the MCP surface uses to call the Control Plane (Tier 2).

The client is intentionally dumb: it marshals envelope types to/from JSON and
raises on non-2xx. Status-code handling:

- 2xx  → parse the body into the declared response model and return it.
- 501  → ``NotImplementedError`` (``detail`` as message). A handful of
         composer-/HPC-dependent routes still stub with 501 so MCP tools can
         render a clear "not built yet" message to Claude Desktop until the
         blocking task lands.
- other non-2xx → ``httpx.HTTPStatusError`` via ``raise_for_status()``. Callers
         handle 404/409 as appropriate (e.g. double-approve a decided approval
         → 409, unknown approval_id → 404).

Real-backed endpoints as of TX1 merge: create_approval, approve, reject,
correct, list_pending_approvals, list_runs, get_status, get_artifact.
Still stubbed (501): start_workflow, generate_plan, show_yaml_diff
(blocked by composer / T06 differ).

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
    CorrectRequest,
    CreateApprovalRequest,
    CreateApprovalResponse,
    CreateVerifiedSynonymRequest,
    GeneratePlanRequest,
    GeneratePlanResponse,
    GetArtifactRequest,
    GetArtifactResponse,
    GetStatusRequest,
    GetStatusResponse,
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


class ControlPlaneClient:
    """HTTP client to the Control Plane.

    Round 3: HPC endpoints are intentionally omitted from this client. They are
    exercised only by the optional HPC-export lane; the MCP surface consumes
    them through a separate optional extension.
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
