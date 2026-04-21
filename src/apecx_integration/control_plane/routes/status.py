"""Run, step, and artifact inspection routes (TX1)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from apecx_integration.control_plane.schemas.api import (
    GetArtifactRequest,
    GetArtifactResponse,
    GetStatusRequest,
    GetStatusResponse,
    ListRunsRequest,
    ListRunsResponse,
)

router = APIRouter(prefix="/runs", tags=["status"])


def _not_implemented(task_ref: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"not implemented — see implementation_plan.md {task_ref}",
    )


@router.post("/list", response_model=ListRunsResponse)
async def list_runs(body: ListRunsRequest) -> ListRunsResponse:
    raise _not_implemented("T09")


@router.post("/status", response_model=GetStatusResponse)
async def get_status(body: GetStatusRequest) -> GetStatusResponse:
    raise _not_implemented("T09 + T08 (status aggregation)")


@router.post("/artifact", response_model=GetArtifactResponse)
async def get_artifact(body: GetArtifactRequest) -> GetArtifactResponse:
    raise _not_implemented("T09 + T11 (artifact store)")
