"""Optional HPC-export routes (TX1).

Round 3: these tools are only meaningful when the user opts into the HPC export
feature. They remain registered in the API surface so the MCP tool schema is
stable, but they stub with 501 until T04/T05/T07 land.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from apecx_integration.control_plane.schemas.api import (
    ConfirmAllocationRequest,
    ConfirmAllocationResponse,
    EstimateCostRequest,
    EstimateCostResponse,
    ExportHpcBundleRequest,
    ExportHpcBundleResponse,
    SubmitHpcRequest,
    SubmitHpcResponse,
)

router = APIRouter(prefix="/hpc", tags=["hpc"])


def _not_implemented(task_ref: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"not implemented — see implementation_plan.md {task_ref}",
    )


@router.post("/estimate", response_model=EstimateCostResponse)
async def estimate_cost(body: EstimateCostRequest) -> EstimateCostResponse:
    raise _not_implemented("T07 (allocation accounting)")


@router.post("/confirm", response_model=ConfirmAllocationResponse)
async def confirm_allocation(body: ConfirmAllocationRequest) -> ConfirmAllocationResponse:
    raise _not_implemented("T07")


@router.post("/submit", response_model=SubmitHpcResponse)
async def submit_hpc(body: SubmitHpcRequest) -> SubmitHpcResponse:
    raise _not_implemented("T04 (Globus Compute) or T05 (PBS bundle)")


@router.post("/export", response_model=ExportHpcBundleResponse)
async def export_hpc_bundle(body: ExportHpcBundleRequest) -> ExportHpcBundleResponse:
    raise _not_implemented("T05 (PBS bundle generator)")
