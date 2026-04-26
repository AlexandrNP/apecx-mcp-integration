"""HITL approval routes (TX1).

Every route here is backed by the T09 persistence layer. ``create_approval``
is called internally by the ``ApprovalStep`` (T10) when a workflow pauses for
human review. The user-facing tools (approve / reject / correct / list_pending)
are exposed through the MCP surface and call back into these routes.

Approval lifecycle:
    PENDING -> APPROVED                         (approve)
    PENDING -> REJECTED                         (reject)
    PENDING -> APPROVED_WITH_MODIFICATIONS      (correct)
Transitions from a non-PENDING state raise HTTP 409 (already decided).
Every transition writes an APPROVAL_REQUESTED / APPROVAL_DECIDED
provenance event under the owning run's hash chain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apecx_integration.control_plane.dependencies import get_recorder, get_session
from apecx_integration.control_plane.models.entities import Approval as ApprovalORM
from apecx_integration.control_plane.models.entities import Run as RunORM
from apecx_integration.control_plane.models.entities import Step as StepORM
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
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
from apecx_integration.control_plane.schemas.entities import Approval as ApprovalSchema
from apecx_integration.control_plane.schemas.enums import (
    ApprovalStatus,
    ProvenanceEventType,
)

router = APIRouter(prefix="/approvals", tags=["approval"])


@router.get("/{approval_id}", response_model=ApprovalResponse)
def get_approval(
    approval_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> ApprovalResponse:
    """Return the current state of an approval.

    Called by the nanobrain ``ApprovalStep`` (T10) to poll for a
    decision while it's paused. Returns 404 if the id is unknown.
    Status transitions from PENDING to one of APPROVED /
    APPROVED_WITH_MODIFICATIONS / REJECTED / AUTO_APPROVED /
    TIMED_OUT; the polling step detects the change and resumes.
    """
    approval = _load_approval_or_404(session, approval_id)
    return ApprovalResponse(approval=ApprovalSchema.model_validate(approval))


def _load_approval_or_404(session: Session, approval_id: UUID) -> ApprovalORM:
    approval = session.get(ApprovalORM, approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"approval {approval_id} not found",
        )
    return approval


def _require_pending(approval: ApprovalORM) -> None:
    if approval.status is not ApprovalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"approval {approval.id} already decided (status={approval.status.value})",
        )


def _run_id_for_step(session: Session, step_id: UUID) -> UUID:
    run_id = session.execute(
        select(StepORM.run_id).where(StepORM.id == step_id)
    ).scalar_one_or_none()
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"step {step_id} not found",
        )
    return run_id


@router.post("/", response_model=CreateApprovalResponse)
def create_approval(
    body: CreateApprovalRequest,
    session: Annotated[Session, Depends(get_session)],
    recorder: Annotated[ProvenanceRecorder, Depends(get_recorder)],
) -> CreateApprovalResponse:
    run_id_for_step = _run_id_for_step(session, body.step_id)
    if run_id_for_step != body.run_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"step {body.step_id} belongs to run {run_id_for_step}, "
                f"not the run_id {body.run_id} in the request"
            ),
        )
    approval = ApprovalORM(
        step_id=body.step_id,
        kind=body.kind,
        status=ApprovalStatus.PENDING,
        policy={
            "summary": body.summary,
            "artifact_ids": [str(aid) for aid in body.artifact_ids],
            **body.policy,
        },
    )
    session.add(approval)
    session.commit()
    session.refresh(approval)
    recorder.record(
        run_id=body.run_id,
        event_type=ProvenanceEventType.APPROVAL_REQUESTED,
        actor="control_plane",
        payload={
            "approval_id": str(approval.id),
            "step_id": str(body.step_id),
            "kind": body.kind.value,
            "summary": body.summary,
        },
    )
    return CreateApprovalResponse(approval=ApprovalSchema.model_validate(approval))


def _decide(
    session: Session,
    recorder: ProvenanceRecorder,
    approval_id: UUID,
    new_status: ApprovalStatus,
    *,
    decided_by: str,
    comment: str | None,
    extra_policy: dict[str, object] | None = None,
) -> ApprovalORM:
    approval = _load_approval_or_404(session, approval_id)
    _require_pending(approval)  # fast-path 409 on the obvious race
    run_id = _run_id_for_step(session, approval.step_id)

    # Audit-trail integrity (cluster V1, found 2026-04-25):
    # the read-then-mutate-then-commit pattern is racy under
    # concurrent approve+reject. Two coroutines on different
    # sessions both load the approval, both pass _require_pending
    # (each sees PENDING from its own snapshot), both set status,
    # both commit (the second wins on the row), and both proceed
    # to recorder.record() — yielding TWO APPROVAL_DECIDED events
    # with conflicting status values for the same approval id.
    #
    # The hash-chain audit's whole purpose is to make that kind
    # of contradiction impossible. Mirror cluster E's atomic
    # conditional UPDATE: WHERE status='PENDING' so only the
    # first writer's UPDATE matches a row; the loser sees
    # rowcount=0, rolls back, and raises 409 BEFORE recording
    # any provenance event.
    new_decided_at = datetime.now(UTC)
    update_stmt = (
        update(ApprovalORM)
        .where(
            ApprovalORM.id == approval_id,
            ApprovalORM.status == ApprovalStatus.PENDING,
        )
        .values(
            status=new_status,
            decided_by=decided_by,
            decided_at=new_decided_at,
            comment=(
                comment
                if comment is not None
                else approval.comment
            ),
            policy=(
                {**approval.policy, **extra_policy}
                if extra_policy
                else approval.policy
            ),
        )
    )
    result = session.execute(update_stmt)
    if result.rowcount != 1:
        # Concurrent decision beat us. Don't record an event — the
        # winner already did. Surface a clear 409 to the loser.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Approval {approval_id} was decided concurrently by "
                "another caller; this transition lost the race. "
                "Re-fetch and retry."
            ),
        )
    session.commit()
    # Re-read so the response reflects the committed state.
    approval = _load_approval_or_404(session, approval_id)
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.APPROVAL_DECIDED,
        actor=decided_by,
        payload={
            "approval_id": str(approval.id),
            "status": new_status.value,
            "comment": comment or "",
        },
    )
    return approval


@router.post("/approve", response_model=ApprovalResponse)
def approve(
    body: ApproveRequest,
    session: Annotated[Session, Depends(get_session)],
    recorder: Annotated[ProvenanceRecorder, Depends(get_recorder)],
) -> ApprovalResponse:
    approval = _decide(
        session,
        recorder,
        body.approval_id,
        ApprovalStatus.APPROVED,
        decided_by=body.decided_by,
        comment=body.comment or None,
    )
    return ApprovalResponse(approval=ApprovalSchema.model_validate(approval))


@router.post("/reject", response_model=ApprovalResponse)
def reject(
    body: RejectRequest,
    session: Annotated[Session, Depends(get_session)],
    recorder: Annotated[ProvenanceRecorder, Depends(get_recorder)],
) -> ApprovalResponse:
    approval = _decide(
        session,
        recorder,
        body.approval_id,
        ApprovalStatus.REJECTED,
        decided_by=body.decided_by,
        comment=body.reason,
    )
    return ApprovalResponse(approval=ApprovalSchema.model_validate(approval))


@router.post("/correct", response_model=ApprovalResponse)
def correct(
    body: CorrectRequest,
    session: Annotated[Session, Depends(get_session)],
    recorder: Annotated[ProvenanceRecorder, Depends(get_recorder)],
) -> ApprovalResponse:
    approval = _decide(
        session,
        recorder,
        body.approval_id,
        ApprovalStatus.APPROVED_WITH_MODIFICATIONS,
        decided_by=body.decided_by,
        comment=None,
        extra_policy={"modifications": body.modifications},
    )
    return ApprovalResponse(approval=ApprovalSchema.model_validate(approval))


@router.post("/pending", response_model=ListPendingApprovalsResponse)
def list_pending_approvals(
    body: ListPendingApprovalsRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ListPendingApprovalsResponse:
    rows = (
        session.execute(
            select(ApprovalORM)
            .join(StepORM, ApprovalORM.step_id == StepORM.id)
            .join(RunORM, StepORM.run_id == RunORM.id)
            .where(
                ApprovalORM.status == ApprovalStatus.PENDING,
                RunORM.user_id == body.user_id,
            )
            .order_by(ApprovalORM.id)
        )
        .scalars()
        .all()
    )
    return ListPendingApprovalsResponse(approvals=[ApprovalSchema.model_validate(r) for r in rows])
