"""Optional HPC-export routes (TX1).

Round 3: these tools are only meaningful when the user opts into the HPC export
feature. They remain registered in the API surface so the MCP tool schema is
stable. T07 (/hpc/estimate) landed 2026-04-23; the rest still stub with 501
until T04/T05 land.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from apecx_integration.control_plane.accounting.cost_estimator import (
    estimate_workflow_cost,
)
from apecx_integration.control_plane.dependencies import get_session
from apecx_integration.control_plane.models.entities import (
    Artifact as ArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    Run as RunORM,
)
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


# Endpoint is hardcoded to "local" for T07 Phase 1. When T04 (Globus
# Compute) or T05 (PBS bundle) land, they'll add endpoint selection to
# the request schema; until then, every estimate targets "local" with
# factor 1.0 (no pricing effect). Not over-engineering a selector when
# there's nothing to select.
_DEFAULT_ENDPOINT: str = "local"


@router.post("/estimate", response_model=EstimateCostResponse)
async def estimate_cost(
    body: EstimateCostRequest,
    session: Annotated[Session, Depends(get_session)],
) -> EstimateCostResponse:
    """Pre-submission allocation estimate for a run's workflow.

    Fetches the run's ``workflow_config_id`` Artifact, loads the YAML,
    feeds it to ``estimate_workflow_cost``, returns the result.

    Error cases:
    - 404 if the run doesn't exist.
    - 422 if the run has no workflow_config_id (can't estimate against
      nothing).
    - 404 if the Artifact row exists but its on-disk file is missing
      (tamper / manual-delete scenario).
    - 422 if the Artifact yaml is malformed.
    """
    run = session.get(RunORM, body.run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {body.run_id} not found",
        )
    if run.workflow_config_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Run {body.run_id} has no workflow_config_id — nothing "
                "to estimate against. Compose a workflow (/workflows/plan) "
                "or attach one to the run before calling /hpc/estimate."
            ),
        )

    artifact = session.get(ArtifactORM, run.workflow_config_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Run {body.run_id} references Artifact "
                f"{run.workflow_config_id} which does not exist."
            ),
        )

    on_disk = Path(artifact.location)
    if not on_disk.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Artifact {artifact.id} row exists but its on-disk file "
                f"{on_disk} is missing. Artifacts are append-only; someone "
                "bypassed the API."
            ),
        )

    try:
        workflow_dict = yaml.safe_load(on_disk.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Artifact {artifact.id} is not valid YAML: {exc}. This is "
                "a data-integrity issue — the artifact should have been "
                "validated at composer time."
            ),
        ) from exc

    if not isinstance(workflow_dict, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Artifact {artifact.id} top-level must be a mapping; "
                f"got {type(workflow_dict).__name__}."
            ),
        )

    try:
        return estimate_workflow_cost(workflow_dict, endpoint=_DEFAULT_ENDPOINT)
    except ValueError as exc:
        # The estimator raises ValueError on a missing/non-mapping
        # ``steps:`` block. Surface as 422 — the artifact is structurally
        # wrong, not a server fault.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post("/confirm", response_model=ConfirmAllocationResponse)
async def confirm_allocation(body: ConfirmAllocationRequest) -> ConfirmAllocationResponse:
    raise _not_implemented("T07")


@router.post("/submit", response_model=SubmitHpcResponse)
async def submit_hpc(body: SubmitHpcRequest) -> SubmitHpcResponse:
    raise _not_implemented("T04 (Globus Compute) or T05 (PBS bundle)")


@router.post("/export", response_model=ExportHpcBundleResponse)
async def export_hpc_bundle(body: ExportHpcBundleRequest) -> ExportHpcBundleResponse:
    raise _not_implemented("T05 (PBS bundle generator)")
