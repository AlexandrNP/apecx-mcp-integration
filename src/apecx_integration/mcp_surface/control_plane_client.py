"""Thin HTTP client the MCP surface uses to call the Control Plane (Tier 2).

The client is intentionally dumb: it marshals envelope types to/from JSON and
raises on non-2xx (except 501, which it surfaces as NotImplementedError so MCP
tools can render a clear "not built yet" message to Claude Desktop during
early phases).

See architectural_plan.md §3.2 (why HTTP between Tier 1 and Tier 2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from apecx_integration.control_plane.schemas.api import (
    ApprovalResponse,
    ApproveRequest,
    CorrectRequest,
    CreateApprovalRequest,
    CreateApprovalResponse,
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
    ShowYamlDiffRequest,
    ShowYamlDiffResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
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
