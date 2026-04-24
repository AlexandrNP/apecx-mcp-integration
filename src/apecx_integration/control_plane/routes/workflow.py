"""Workflow-creation and plan-inspection routes (TX1).

Two endpoints still stub 501 — ``/workflows/start`` depends on T09
run-lifecycle wiring and ``/workflows/plan`` depends on the composer
being invoked directly from the API (currently only via
``Composer.compose`` in-process). T06 landed ``/workflows/diff``
2026-04-22.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from apecx_integration.control_plane.dependencies import get_session
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
    GeneratePlanRequest,
    GeneratePlanResponse,
    ShowYamlDiffRequest,
    ShowYamlDiffResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
    StepPlan,
)
from apecx_integration.control_plane.schemas.enums import StepCategory

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
