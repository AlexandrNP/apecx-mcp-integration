"""HITL approval routes (TX1).

``create_approval`` is called internally by the ``ApprovalStep`` (T10) when a
workflow pauses for human review — for example, when the LLM proposes synonyms
and the user must approve or correct them.

The user-facing tools (approve / reject / correct / list_pending) are exposed
through the MCP surface (Tier 1) and call back into these routes.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from apecx_integration.control_plane.schemas.api import (
    ApprovalResponse,
    ApproveRequest,
    CorrectRequest,
    CreateApprovalRequest,
    CreateApprovalResponse,
    ListPendingApprovalsRequest,
    ListPendingApprovalsResponse,
    RejectRequest,
)

router = APIRouter(prefix="/approvals", tags=["approval"])


def _not_implemented(task_ref: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"not implemented — see implementation_plan.md {task_ref}",
    )


@router.post("/", response_model=CreateApprovalResponse)
async def create_approval(body: CreateApprovalRequest) -> CreateApprovalResponse:
    raise _not_implemented("T09 + T10 (ApprovalStep calls this endpoint)")


@router.post("/approve", response_model=ApprovalResponse)
async def approve(body: ApproveRequest) -> ApprovalResponse:
    raise _not_implemented("T09 + T10")


@router.post("/reject", response_model=ApprovalResponse)
async def reject(body: RejectRequest) -> ApprovalResponse:
    raise _not_implemented("T09 + T10")


@router.post("/correct", response_model=ApprovalResponse)
async def correct(body: CorrectRequest) -> ApprovalResponse:
    raise _not_implemented("T09 + T10 (correction dispatch to step input)")


@router.post("/pending", response_model=ListPendingApprovalsResponse)
async def list_pending_approvals(
    body: ListPendingApprovalsRequest,
) -> ListPendingApprovalsResponse:
    raise _not_implemented("T09")
