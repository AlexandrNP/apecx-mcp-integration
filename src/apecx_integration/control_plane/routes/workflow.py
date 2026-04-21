"""Workflow-creation and plan-inspection routes (TX1).

Every handler currently raises ``HTTPException(501)`` with a pointer to the
implementation_plan.md task that must land before the handler can do real work.
The shapes are final (derived from schemas.api); only the bodies are stubs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from apecx_integration.control_plane.schemas.api import (
    GeneratePlanRequest,
    GeneratePlanResponse,
    ShowYamlDiffRequest,
    ShowYamlDiffResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
)

router = APIRouter(prefix="/workflows", tags=["workflow"])


def _not_implemented(task_ref: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"not implemented — see implementation_plan.md {task_ref}",
    )


@router.post("/start", response_model=StartWorkflowResponse)
async def start_workflow(body: StartWorkflowRequest) -> StartWorkflowResponse:
    raise _not_implemented("T09 (run persistence) + composer")


@router.post("/plan", response_model=GeneratePlanResponse)
async def generate_plan(body: GeneratePlanRequest) -> GeneratePlanResponse:
    raise _not_implemented("composer (Phase 2) + T02 (library wrappers)")


@router.post("/diff", response_model=ShowYamlDiffResponse)
async def show_yaml_diff(body: ShowYamlDiffRequest) -> ShowYamlDiffResponse:
    raise _not_implemented("T06 (differential-review UX)")
