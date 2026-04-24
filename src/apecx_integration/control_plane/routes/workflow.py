"""Workflow-creation and plan-inspection routes.

- ``/workflows/start``  — T01 P1: composes a workflow, creates a Run
                          row + Artifact via the composer, sets
                          status=PAUSED or RUNNING per T06 policy.
- ``/workflows/plan``   — still 501 (no standalone use case yet —
                          /workflows/start covers prompt → composed).
- ``/workflows/diff``   — T06 2026-04-22.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from apecx_integration.composition.approval_policy import ApprovalPolicy
from apecx_integration.composition.composer import Composer
from apecx_integration.control_plane.dependencies import (
    get_approval_policy,
    get_composer,
    get_local_executor,
    get_session,
)
from apecx_integration.control_plane.executors.local import LocalExecutor
from apecx_integration.control_plane.models.entities import (
    Artifact as ArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    GeneratedArtifact as GeneratedArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    Run as RunORM,
)
from apecx_integration.control_plane.schemas.api import (
    ExecuteWorkflowRequest,
    ExecuteWorkflowResponse,
    GeneratePlanRequest,
    GeneratePlanResponse,
    ShowYamlDiffRequest,
    ShowYamlDiffResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
    StepPlan,
)
from apecx_integration.control_plane.schemas.entities import Run as RunSchema
from apecx_integration.control_plane.schemas.enums import (
    RunStatus,
    StepCategory,
)

router = APIRouter(prefix="/workflows", tags=["workflow"])


def _not_implemented(task_ref: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"not implemented — see implementation_plan.md {task_ref}",
    )


@router.post("/start", response_model=StartWorkflowResponse)
async def start_workflow(
    body: StartWorkflowRequest,
    session: Annotated[Session, Depends(get_session)],
    composer: Annotated[Composer, Depends(get_composer)],
    policy: Annotated[ApprovalPolicy, Depends(get_approval_policy)],
) -> StartWorkflowResponse:
    """T01 P1: compose a workflow, persist it, return the Run.

    Flow:
    1. Create Run row (status=PENDING). Commit before calling composer
       so the Artifact FK at compose time resolves.
    2. ``composer.compose(description, context={"run_id": ...})`` —
       generates YAML + novel Python, runs the T13 scanner + T06
       categorization, persists via the injected ArtifactStore.
    3. Back-link Run.workflow_config_id.
    4. Evaluate the T06 approval policy against the categorization
       and set run.status:
         - policy AUTO             → RUNNING (ready for execution)
         - REQUIRE_REVIEW / EXPERT → PAUSED (awaits reviewer)
       No Approval ORM row is created here — approvals are
       step-scoped runtime events (ApprovalStep, T10). Clients
       discover pre-execution review gating via
       ``/workflows/diff`` + the Run's PAUSED status.

    **Not covered here (T01 Phase 2 scope):** actually executing the
    composed workflow locally. That needs a Tier 4 local executor
    wired into the Control Plane — see implementation_plan.md §T01
    steps 5-8.
    """
    from apecx_integration.composition.approval_policy import ApprovalAction
    from apecx_integration.composition.differ import CategorizedWorkflow

    run_id = uuid4()
    now = datetime.now(UTC)
    run = RunORM(
        id=run_id,
        user_id=body.user_id,
        status=RunStatus.PENDING,
        created_at=now,
    )
    session.add(run)
    session.commit()

    composed = await composer.compose(
        body.description,
        context={"run_id": run_id},
    )

    # Re-read from this session: the composer wrote via its own
    # session, so we need to fetch the Run and back-link it.
    run = session.get(RunORM, run_id)
    assert run is not None, "Run row disappeared between commits"
    run.workflow_config_id = composed.artifact_id

    categorized = CategorizedWorkflow(
        categorizations=composed.composition_summary.step_categorizations,
    )
    decision = policy.evaluate(categorized)
    if decision.strongest_required_action is ApprovalAction.AUTO:
        run.status = RunStatus.RUNNING
        run.started_at = now
    else:
        run.status = RunStatus.PAUSED

    session.commit()
    session.refresh(run)

    return StartWorkflowResponse(
        run=RunSchema.model_validate(run),
        generated_workflow_artifact_id=composed.artifact_id,
    )


@router.post("/plan", response_model=GeneratePlanResponse)
async def generate_plan(
    body: GeneratePlanRequest,
    session: Annotated[Session, Depends(get_session)],
    composer: Annotated[Composer, Depends(get_composer)],
) -> GeneratePlanResponse:
    """Preview-mode composition — same composer flow as
    ``/workflows/start`` but the Run is immediately marked CANCELLED.

    The caller gets a ``GeneratePlanResponse`` with the generated
    YAML, a per-step plan (reusing the T06 categorization), and the
    artifact UUID. Semantically this is "show me what you would
    compose for this prompt; I'm not committing to it yet." The
    CANCELLED Run row stays in the DB so the artifact's FK resolves
    and provenance is intact, but nothing downstream picks it up.

    Why the Run exists at all: ArtifactStore.store() requires a Run
    FK. Rather than grow the schema with a "preview" Run variant,
    we reuse the normal path and flip status post-hoc. The stray
    CANCELLED rows are discoverable by ``user_id='_preview'`` for
    anyone who wants to prune them.
    """
    run_id = uuid4()
    now = datetime.now(UTC)
    run = RunORM(
        id=run_id,
        user_id="_preview",
        status=RunStatus.PENDING,
        created_at=now,
    )
    session.add(run)
    session.commit()

    composed = await composer.compose(
        body.description, context={"run_id": run_id}
    )

    run = session.get(RunORM, run_id)
    assert run is not None, "Run row disappeared between commits"
    run.workflow_config_id = composed.artifact_id
    run.status = RunStatus.CANCELLED
    run.completed_at = now
    session.commit()

    plan: list[StepPlan] = []
    for s in composed.composition_summary.step_categorizations:
        plan.append(
            StepPlan(
                step_id=s.step_id,
                step_name=s.step_id,
                category=StepCategory(s.category.value),
                reference_component_id=s.step_class or None,
                rationale=s.reason,
            )
        )

    return GeneratePlanResponse(
        plan=plan,
        yaml_text=composed.yaml_bytes.decode("utf-8"),
        generated_artifact_id=composed.artifact_id,
    )


@router.post("/execute", response_model=ExecuteWorkflowResponse)
async def execute_workflow(
    body: ExecuteWorkflowRequest,
    executor: Annotated[LocalExecutor, Depends(get_local_executor)],
) -> ExecuteWorkflowResponse:
    """T01 P2 HTTP surface — run a composed workflow to terminal state.

    Synchronous: holds the HTTP connection until the LocalExecutor
    returns. That's fine at first-release scale (workflows are short
    and the operator is watching) but should move to a worker queue
    once workflows run for minutes — track the gap explicitly in the
    plan when it matters.

    Returns the TERMINAL state (COMPLETED or FAILED), never the
    in-flight RUNNING state. A 200 means the executor ran to
    completion *of some kind*, not that the workflow succeeded.
    The caller checks ``status`` on the response to branch.

    503 when the Control Plane was built without a local_executor
    (no route-specific error-catching otherwise — the executor owns
    run-state transitions and is the right place to surface failures).
    """
    result = await executor.execute(body.run_id)
    return ExecuteWorkflowResponse(
        run_id=result.run_id,
        status=result.status,
        reason=result.reason,
        output_artifact_id=result.output_artifact_id,
    )


@router.post("/diff", response_model=ShowYamlDiffResponse)
async def show_yaml_diff(
    body: ShowYamlDiffRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ShowYamlDiffResponse:
    """T06 / AP §5.6 — surface the diff payload for a run's workflow.

    Reads the GENERATED_WORKFLOW Artifact the run points at, the
    GeneratedArtifact JSON sidecar that holds the categorization, and
    the on-disk YAML. No re-running of retrieval — everything needed
    was persisted at compose() time (composer.py ``_persist_or_synthesize``).

    Error cases mirror ``/hpc/estimate`` (T07) for consistency:
    - 404 run unknown
    - 422 run has no workflow_config_id
    - 404 artifact row exists but on-disk YAML missing
    - 422 generated_artifact row missing (older run without T06
      metadata)
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
                f"Run {body.run_id} has no workflow_config_id — "
                "nothing to diff."
            ),
        )
    artifact = session.get(ArtifactORM, run.workflow_config_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Run {body.run_id}.workflow_config_id points at an "
                "artifact row that does not exist."
            ),
        )
    on_disk = Path(artifact.location)
    if not on_disk.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Artifact {artifact.id} on-disk file at {on_disk} "
                "is missing — tamper / manual delete?"
            ),
        )
    yaml_text = on_disk.read_text(encoding="utf-8")

    generated = session.get(GeneratedArtifactORM, artifact.id)
    if generated is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Artifact {artifact.id} has no GeneratedArtifact row "
                "— cannot diff. (Older runs without T06 metadata?)"
            ),
        )

    summary: dict = generated.composition_summary or {}
    cat_rows = summary.get("step_categorizations") or []
    novel_by_step = summary.get("novel_python_by_step") or {}

    plan: list[StepPlan] = []
    for row in cat_rows:
        try:
            category = StepCategory(row["category"])
        except (KeyError, ValueError):
            continue
        plan.append(
            StepPlan(
                step_id=row.get("step_id", ""),
                step_name=row.get("step_id", ""),
                category=category,
                reference_component_id=(
                    row.get("step_class") or None
                ),
                rationale=row.get("reason", ""),
            )
        )

    summary_sentence = summary.get("summary_sentence") or (
        "No summary available for this workflow."
    )

    return ShowYamlDiffResponse(
        yaml_text=yaml_text,
        novel_python_by_step=novel_by_step,
        categorization=plan,
        summary_sentence=summary_sentence,
    )
