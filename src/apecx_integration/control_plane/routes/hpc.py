"""Optional HPC-export routes (TX1).

Round 3: these tools are only meaningful when the user opts into the HPC export
feature. They remain registered in the API surface so the MCP tool schema is
stable. T07 (/hpc/estimate + /hpc/confirm) landed 2026-04-22; /hpc/submit
and /hpc/export still stub with 501 until T04/T05 land (HPC export lane is
demoted optional).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apecx_integration.control_plane.accounting.cost_estimator import (
    estimate_workflow_cost,
)
from apecx_integration.control_plane.dependencies import get_session
from apecx_integration.control_plane.models.entities import (
    AllocationEstimate as AllocationEstimateORM,
)
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
        estimate = estimate_workflow_cost(
            workflow_dict, endpoint=_DEFAULT_ENDPOINT
        )
    except ValueError as exc:
        # The estimator raises ValueError on a missing/non-mapping
        # ``steps:`` block. Surface as 422 — the artifact is structurally
        # wrong, not a server fault.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # Persist the estimate so /hpc/confirm has something to flip. Each
    # estimate call writes a new row — we don't update in place — so
    # the audit trail shows every pre-submission look the user took.
    session.add(
        AllocationEstimateORM(
            id=uuid4(),
            run_id=body.run_id,
            estimated_core_hours=estimate.total_core_hours,
            estimated_wall_time_seconds=estimate.total_core_hours * 3600.0,
            estimated_memory_gb=None,
            endpoint=estimate.endpoint,
            user_confirmed=False,
            user_confirmed_at=None,
            actual_core_hours=None,
        )
    )
    session.commit()

    return estimate


@router.post("/confirm", response_model=ConfirmAllocationResponse)
async def confirm_allocation(
    body: ConfirmAllocationRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ConfirmAllocationResponse:
    """User acknowledges the estimated core-hours for a run.

    Contract:
    - 404 if run unknown.
    - 422 if no prior estimate exists for the run (``/hpc/estimate`` has
      never been called, or all estimate rows were purged).
    - 422 if ``confirmed_core_hours`` is less than the most-recent
      estimated value (user is trying to short-pay the allocation).

    Side effect: sets ``user_confirmed=true`` + ``user_confirmed_at``
    on the latest AllocationEstimate row for the run. Previous rows
    stay untouched — they're the audit trail of "every time the user
    looked at the estimate".

    What this does NOT do: actually submit anything. HPC submission
    lives behind ``/hpc/submit`` (still 501 until T04/T05 land).
    """
    run = session.get(RunORM, body.run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {body.run_id} not found",
        )

    latest = session.execute(
        select(AllocationEstimateORM)
        .where(AllocationEstimateORM.run_id == body.run_id)
        .order_by(AllocationEstimateORM.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Run {body.run_id} has no AllocationEstimate row. "
                "Call /hpc/estimate before /hpc/confirm."
            ),
        )

    if body.confirmed_core_hours < latest.estimated_core_hours:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"confirmed_core_hours ({body.confirmed_core_hours}) < "
                f"latest estimate ({latest.estimated_core_hours}). The "
                "confirmation ceiling must cover the estimate."
            ),
        )

    latest.user_confirmed = True
    latest.user_confirmed_at = datetime.now(UTC)
    session.commit()

    return ConfirmAllocationResponse(
        run_id=body.run_id, confirmed=True
    )


@router.post("/submit", response_model=SubmitHpcResponse)
async def submit_hpc(body: SubmitHpcRequest) -> SubmitHpcResponse:
    raise _not_implemented("T04 (Globus Compute) or T05 (PBS bundle)")


@router.post("/export", response_model=ExportHpcBundleResponse)
async def export_hpc_bundle(
    body: ExportHpcBundleRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ExportHpcBundleResponse:
    """T05 — produce a qsub-able PBS bundle on disk for a Run.

    Error classes mirror /hpc/estimate; the generator itself raises
    ``UnsupportedSystem`` for target_system outside {polaris, aurora}.

    The endpoint does NOT submit via qsub. Scientist runs qsub
    manually; Tier 2 re-ingest on completion consumes
    ``provenance_seed.json`` inside the bundle.
    """
    from apecx_integration.control_plane.models.entities import (
        GeneratedArtifact as GeneratedArtifactORM,
    )
    from apecx_integration.execution.pbs_bundle import (
        BundleRequest,
        UnsupportedSystem,
        generate_bundle,
    )

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
                f"Run {body.run_id} has no workflow_config_id — "
                "nothing to export. Compose a workflow first."
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
                f"Artifact {artifact.id} row exists but its on-disk "
                f"file {on_disk} is missing."
            ),
        )
    generated = session.get(GeneratedArtifactORM, artifact.id)
    if generated is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Artifact {artifact.id} has no GeneratedArtifact row; "
                "cannot export without composition metadata."
            ),
        )

    summary: dict = generated.composition_summary or {}
    try:
        result = generate_bundle(
            BundleRequest(
                run_id=body.run_id,
                target_system=body.target_system,
                output_directory=Path(body.output_directory),
                workflow_yaml_path=on_disk,
                library_version=generated.library_version,
                llm_model=generated.llm_model,
                artifact_id=artifact.id,
                composition_summary_sentence=summary.get(
                    "summary_sentence", ""
                ),
            )
        )
    except UnsupportedSystem as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ExportHpcBundleResponse(
        bundle_path=str(result.bundle_path),
        submit_command=result.submit_command,
    )
