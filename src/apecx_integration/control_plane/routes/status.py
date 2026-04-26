"""Run, step, and artifact inspection routes (TX1).

All three endpoints are now wired to the T09 persistence layer. Artifact
inline-bytes retrieval requires T11 (artifact store); until then
``get_artifact`` returns the Artifact metadata row with
``inline_bytes=None`` and a ``reason_inline_omitted`` message.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apecx_integration.control_plane.dependencies import get_session
from apecx_integration.control_plane.models.entities import Approval as ApprovalORM
from apecx_integration.control_plane.models.entities import Artifact as ArtifactORM
from apecx_integration.control_plane.models.entities import Run as RunORM
from apecx_integration.control_plane.models.entities import Step as StepORM
from apecx_integration.control_plane.schemas.api import (
    GetArtifactRequest,
    GetArtifactResponse,
    GetStatusRequest,
    GetStatusResponse,
    ListRunsRequest,
    ListRunsResponse,
)
from apecx_integration.control_plane.schemas.entities import (
    Approval as ApprovalSchema,
)
from apecx_integration.control_plane.schemas.entities import (
    Artifact as ArtifactSchema,
)
from apecx_integration.control_plane.schemas.entities import (
    Run as RunSchema,
)
from apecx_integration.control_plane.schemas.entities import (
    Step as StepSchema,
)
from apecx_integration.control_plane.schemas.enums import ApprovalStatus

router = APIRouter(prefix="/runs", tags=["status"])


@router.post("/list", response_model=ListRunsResponse)
def list_runs(
    body: ListRunsRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ListRunsResponse:
    stmt = select(RunORM).where(RunORM.user_id == body.user_id)
    if body.status_filter is not None:
        stmt = stmt.where(RunORM.status == body.status_filter)
    stmt = stmt.order_by(RunORM.created_at.desc()).limit(body.limit)
    runs = session.execute(stmt).scalars().all()
    return ListRunsResponse(runs=[RunSchema.model_validate(r) for r in runs])


@router.post("/status", response_model=GetStatusResponse)
def get_status(
    body: GetStatusRequest,
    session: Annotated[Session, Depends(get_session)],
) -> GetStatusResponse:
    run = session.get(RunORM, body.run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run {body.run_id} not found",
        )
    steps = (
        session.execute(
            select(StepORM)
            .where(StepORM.run_id == body.run_id)
            # Order by start time when the step has run, falling
            # back to creation time for PENDING steps. ``id`` is a
            # random uuid4, so falling back to it scrambled PENDING
            # steps (cluster AH, 2026-04-26). Migration 0006 added
            # ``created_at``; tiebreak on id only for the unlikely
            # tied-microsecond case.
            .order_by(
                StepORM.started_at.asc().nulls_last(),
                StepORM.created_at.asc(),
                StepORM.id,
            )
        )
        .scalars()
        .all()
    )
    pending_approval = session.execute(
        select(ApprovalORM)
        .join(StepORM, ApprovalORM.step_id == StepORM.id)
        .where(
            StepORM.run_id == body.run_id,
            ApprovalORM.status == ApprovalStatus.PENDING,
        )
        # FIFO: pick the OLDEST pending approval to surface, not
        # the lex-smallest UUID. Same shape as cluster AE's fix to
        # /approvals/pending but for this single-result picker.
        .order_by(ApprovalORM.created_at, ApprovalORM.id)
        .limit(1)
    ).scalar_one_or_none()
    return GetStatusResponse(
        run=RunSchema.model_validate(run),
        steps=[StepSchema.model_validate(s) for s in steps],
        pending_approval=(
            ApprovalSchema.model_validate(pending_approval)
            if pending_approval is not None
            else None
        ),
    )


@router.post("/artifact", response_model=GetArtifactResponse)
def get_artifact(
    body: GetArtifactRequest,
    session: Annotated[Session, Depends(get_session)],
) -> GetArtifactResponse:
    artifact = session.get(ArtifactORM, body.artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"artifact {body.artifact_id} not found",
        )
    return GetArtifactResponse(
        artifact=ArtifactSchema.model_validate(artifact),
        inline_bytes=None,
        reason_inline_omitted=(
            "T11 artifact store not yet landed — inline bytes unavailable; "
            "resolve via artifact.location until then"
        ),
    )
