"""Optional HPC-export routes.

Round 3: these tools only matter when the user opts into the HPC
export lane. They remain registered for MCP tool-schema stability.

Route status (2026-04-22):
    /hpc/estimate    T07 pre-submission cost (live)
    /hpc/confirm     T07 user-acknowledgement gate (live)
    /hpc/export      T05 PBS bundle generator (live)
    /hpc/ingest      T05 AC3 tier-2 reconciliation (live)
    /hpc/submit      **still 501** — genuinely blocked on T04/T05
                     runtime (qsub via SSH or Globus SDK; both
                     require operator access to an HPC endpoint).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apecx_integration.control_plane.accounting.cost_estimator import (
    estimate_workflow_cost,
)
from apecx_integration.control_plane.dependencies import get_recorder, get_session
from apecx_integration.control_plane.models.entities import (
    AllocationEstimate as AllocationEstimateORM,
)
from apecx_integration.control_plane.models.entities import (
    Artifact as ArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    Run as RunORM,
)
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.api import (
    ConfirmAllocationRequest,
    ConfirmAllocationResponse,
    EstimateCostRequest,
    EstimateCostResponse,
    ExportHpcBundleRequest,
    ExportHpcBundleResponse,
    IngestHpcBundleRequest,
    IngestHpcBundleResponse,
    SubmitHpcRequest,
    SubmitHpcResponse,
)
from apecx_integration.control_plane.schemas.enums import (
    ArtifactKind,
    ProvenanceEventType,
    RunStatus,
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
            created_at=datetime.now(UTC),
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
        # Migration 0004 added ``created_at`` so we have a real
        # chronological key. ``id`` is a random uuid4 — ordering by
        # it picked the wrong row when UUID lex order disagreed with
        # insertion order. We tiebreak on id only for the unlikely
        # tied-microsecond case (cluster AC, 2026-04-26).
        .order_by(
            AllocationEstimateORM.created_at.desc(),
            AllocationEstimateORM.id.desc(),
        )
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

    # Cluster AK (2026-04-26) — fail-fast against a racing
    # concurrent /hpc/estimate insert. The earlier SELECT picked
    # the latest at SELECT-time; if a NEWER row landed between
    # SELECT and our UPDATE, the user's intent (confirm the
    # latest) cannot be honored: marking the stale row would
    # leave the new row unconfirmed while the API returns 200,
    # which is exactly the silent-failure shape friction-log #19
    # warns against.
    #
    # Conditional UPDATE: target ``latest.id`` AND require that
    # no AllocationEstimate for this run has a strictly newer
    # ``created_at``. rowcount==0 → race detected, 409. Tie on
    # microseconds keeps the behavior idempotent for the same
    # row (the conditional re-read sees latest itself).
    from sqlalchemy import not_, select as _select, exists as _exists

    confirmed_at = datetime.now(UTC)
    newer_exists = (
        _select(AllocationEstimateORM.id)
        .where(AllocationEstimateORM.run_id == body.run_id)
        .where(AllocationEstimateORM.created_at > latest.created_at)
    ).exists()
    result = session.execute(
        update(AllocationEstimateORM)
        .where(AllocationEstimateORM.id == latest.id)
        .where(not_(newer_exists))
        .values(
            user_confirmed=True,
            user_confirmed_at=confirmed_at,
        )
    )
    session.commit()
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {body.run_id}: a newer AllocationEstimate "
                "appeared between this confirm's SELECT and UPDATE. "
                "Refusing to mark a stale row as confirmed (would "
                "leave the actual latest unconfirmed). Re-fetch the "
                "latest estimate (call /hpc/estimate again or read "
                "the run state) and re-confirm."
            ),
        )

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


@router.post("/ingest", response_model=IngestHpcBundleResponse)
async def ingest_hpc_bundle(
    body: IngestHpcBundleRequest,
    session: Annotated[Session, Depends(get_session)],
    recorder: Annotated[ProvenanceRecorder, Depends(get_recorder)],
) -> IngestHpcBundleResponse:
    """T05 AC3 — reconcile a completed bundle back into Tier 2.

    After a scientist qsubs a bundle and it completes on the remote
    system, they transfer the bundle directory back (with
    ``outputs/result.json`` + ``apecx_status.txt`` populated) and
    POST that path here. This route:

    1. Reads ``provenance_seed.json`` to identify the owning Run.
    2. Reads ``apecx_status.txt`` to decide terminal state.
    3. Persists ``outputs/result.json`` as a new OUTPUT Artifact.
    4. Flips the Run to COMPLETED (or FAILED on status=failed).
    5. Emits RUN_COMPLETED / RUN_FAILED provenance events.

    Error classes:
    - 404: bundle path doesn't exist / provenance_seed.json missing
    - 422: provenance_seed malformed; run_id missing or not a UUID
    - 404: run_id in seed doesn't exist in this Control Plane
    - 409: Run is already in a terminal state (no silent re-ingest)
    - 422: outputs/result.json missing + status=completed (contract
      violation — a completed bundle must carry a result)
    """
    from uuid import UUID as UUIDType

    bundle = Path(body.bundle_path)
    if not bundle.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"bundle_path {bundle} does not exist or is not a directory",
        )
    seed_file = bundle / "provenance_seed.json"
    if not seed_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"bundle at {bundle} has no provenance_seed.json — "
                "not an APECx export bundle."
            ),
        )
    try:
        import json as _json
        seed = _json.loads(seed_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"provenance_seed.json malformed: {exc}",
        ) from exc

    raw_run_id = seed.get("run_id")
    try:
        seed_run_id = UUIDType(str(raw_run_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"provenance_seed.json run_id is not a UUID: {raw_run_id!r}",
        ) from exc

    run = session.get(RunORM, seed_run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Run {seed_run_id} from bundle's provenance_seed.json "
                "does not exist in this Control Plane. Bundle was "
                "generated elsewhere?"
            ),
        )

    terminal = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    if run.status in terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {seed_run_id} is already in terminal state "
                f"{run.status.value}; bundle ingest is append-only, "
                "not idempotent."
            ),
        )

    status_file = bundle / "apecx_status.txt"
    reported = (
        status_file.read_text(encoding="utf-8").strip()
        if status_file.is_file()
        else "completed"
    )
    result_file = bundle / "outputs" / "result.json"

    completion_time = datetime.now(UTC)

    # Audit §3.5: the stub bundle (Phase-2 scaffold, T05 follow-up)
    # writes ``stub_completed`` instead of ``completed`` to flag that
    # the actual workflow did NOT execute. Detect that here and refuse
    # to mark the run COMPLETED — otherwise an operator round-tripping
    # a stub bundle would see a green "completed" status downstream
    # despite no real work having been done. Setting the run to FAILED
    # with a clear reason makes the failure mode visible.
    if reported == "stub_completed":
        result = session.execute(
            update(RunORM)
            .where(
                RunORM.id == seed_run_id,
                RunORM.status.notin_(list(terminal)),
            )
            .values(status=RunStatus.FAILED, completed_at=completion_time)
        )
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Run {seed_run_id} reached terminal state from a "
                    "concurrent ingest before this stub bundle could "
                    "be reconciled."
                ),
            )
        session.commit()
        recorder.record(
            run_id=seed_run_id,
            event_type=ProvenanceEventType.RUN_FAILED,
            actor="hpc_ingest",
            payload={
                "bundle_path": str(bundle),
                "reason": "stub_bundle_detected",
                "note": (
                    "Bundle's run.sh wrote 'stub_completed' marker "
                    "(audit §3.5). Phase-2 PBS bundle scaffolds do "
                    "not execute the workflow; T05 runtime is still "
                    "pending. Run marked FAILED so the stub success "
                    "doesn't masquerade as a real terminal state."
                ),
            },
        )
        return IngestHpcBundleResponse(
            run_id=seed_run_id,
            status=RunStatus.FAILED,
            output_artifact_id=None,
        )

    # Audit §2.3: the read-then-write pattern above (line 414 read +
    # line 426 terminal-check + the write below) is racy against a
    # concurrent ingest on the same run — both could see status as
    # non-terminal, then both write conflicting outcomes. The fix is
    # a conditional UPDATE keyed on the current non-terminal state
    # so only one transaction can flip the run; the loser's UPDATE
    # affects 0 rows and is converted to a 409. Works on both SQLite
    # (statements serialize on the WAL writer) and Postgres (row lock
    # acquired during UPDATE). ``completion_time`` was already
    # captured before the stub-bundle branch above.

    if reported == "failed":
        result = session.execute(
            update(RunORM)
            .where(
                RunORM.id == seed_run_id,
                RunORM.status.notin_(list(terminal)),
            )
            .values(status=RunStatus.FAILED, completed_at=completion_time)
        )
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Run {seed_run_id} reached terminal state from a "
                    "concurrent ingest before this one could write — "
                    "another bundle was ingested for the same run."
                ),
            )
        session.commit()
        recorder.record(
            run_id=seed_run_id,
            event_type=ProvenanceEventType.RUN_FAILED,
            actor="hpc_ingest",
            payload={
                "bundle_path": str(bundle),
                "reason": "remote_failure",
            },
        )
        return IngestHpcBundleResponse(
            run_id=seed_run_id,
            status=RunStatus.FAILED,
            output_artifact_id=None,
        )

    # reported in {"completed", "started", anything else} → treat as
    # completed-if-result-present, else 422.
    if not result_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"bundle at {bundle} claims success but has no "
                "outputs/result.json — contract violation."
            ),
        )

    # Persist the result as an OUTPUT Artifact. Content-hash for
    # integrity, same pattern as ArtifactStore writes though we do it
    # inline here because the ingest doesn't go through the generator
    # path.
    import hashlib as _hashlib
    import uuid as _uuid
    content = result_file.read_bytes()
    artifact_id = _uuid.uuid4()
    output_artifact = ArtifactORM(
        id=artifact_id,
        run_id=seed_run_id,
        step_id=None,
        kind=ArtifactKind.OUTPUT,
        location=str(result_file.resolve()),
        content_hash=_hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        mime_type="application/json",
        created_at=completion_time,
    )
    session.add(output_artifact)
    session.flush()  # surface FK or constraint errors before the UPDATE.

    # Same conditional-UPDATE guard as the FAILED branch — atomic
    # against concurrent ingests.
    result = session.execute(
        update(RunORM)
        .where(
            RunORM.id == seed_run_id,
            RunORM.status.notin_(list(terminal)),
        )
        .values(status=RunStatus.COMPLETED, completed_at=completion_time)
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {seed_run_id} reached terminal state from a "
                "concurrent ingest before this one could write — "
                "another bundle was ingested for the same run."
            ),
        )
    session.commit()

    recorder.record(
        run_id=seed_run_id,
        event_type=ProvenanceEventType.RUN_COMPLETED,
        actor="hpc_ingest",
        payload={
            "bundle_path": str(bundle),
            "output_artifact_id": str(artifact_id),
        },
    )

    return IngestHpcBundleResponse(
        run_id=seed_run_id,
        status=RunStatus.COMPLETED,
        output_artifact_id=artifact_id,
    )
