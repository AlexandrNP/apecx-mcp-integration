"""T01 P2 — local executor for composed workflows.

Takes a Run whose status is RUNNING (or PENDING auto-advanced), loads
the generated workflow YAML via ``nanobrain.core.workflow.Workflow.
from_config``, drives it via ``workflow.run({})`` (G8 cascade-aware
entry point), persists the resolved workflow-output dict as an OUTPUT
Artifact, and updates ``run.status`` + emits the corresponding
provenance event.

G35 (2026-05-09): the executor previously called
``workflow.process({})`` which only deposits input into the first
step's data unit and returns immediately while the cascade fires in
background tasks; the persisted artifact then carried the trigger-
init status dict (``{"status": "data_flow_initiated", ...}``)
instead of the workflow's actual output. Every multi-step composed
workflow run through the canonical executor was silently dropping
its outputs. Source: ``eval_03_nanobrain_gap_inventory.md`` Round 4 G35.

Failure contract
----------------
The executor's job is not to succeed — it is to fail *cleanly*. Three
failure modes are captured and marked ``RUN_FAILED`` with a specific
``reason`` payload in the provenance event:

- ``load_failed`` — the composed YAML doesn't load via
  ``Workflow.from_config``. Most commonly because a step's wrapper
  YAML path doesn't resolve, or because a class path references an
  uninstalled package.
- ``execute_failed`` — the workflow loaded but ``process()`` raised.
  Covers the "Ollama unreachable" / "BV-BRC snapshot missing" /
  "step raised" failure class.
- ``workflow_misconfigured`` — the Run state is invalid before we
  even try (no workflow_config_id, artifact on disk missing).

A successful run emits ``RUN_STARTED`` (before load) and
``RUN_COMPLETED`` (after persist). The provenance chain validates
for either path.

Workflow directory resolution
-----------------------------
The composer emits YAML with ``config: "steps/<name>.yml"`` relative
paths. Nanobrain's loader resolves these relative to the YAML file's
own directory. ``LocalExecutor`` stages a run-root directory that
symlinks the configured ``workflow_base_dir/steps`` into the temp
dir and copies the Artifact YAML in, so the relative paths resolve
against a real ``steps/`` tree without mutating the source tree.

Not in scope for P2
-------------------
- Mid-execution pausing for ``ApprovalStep`` (T10 already provides
  the step; wiring its run-state callbacks to the Control Plane is a
  separate closeout).
- Partial-result persistence per step (the AP §5.1 "HITL after first
  step" branch).
- Cancellation mid-run.

These are listed honestly so the next session doesn't rediscover
them the hard way.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.control_plane.models.entities import (
    Artifact as ArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    Run as RunORM,
)
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import (
    ArtifactKind,
    ProvenanceEventType,
    RunStatus,
)

log = logging.getLogger(__name__)


def _format_step_diagnostics(
    started: list[str], done: set[str], failures: list[tuple[str, str, str]]
) -> str:
    """Build a short suffix naming the in-flight and/or failed step(s) from captured G37 events.

    ``started`` = step_start names in order; ``done`` = names that completed OR failed; ``failures``
    = ``(name, exc_type, exc_message)`` triples. Returns ``" <diagnostics>."`` or ``""`` when nothing
    was captured. Pulled out of ``LocalExecutor.run`` so the in-flight / failure naming is
    unit-testable without driving a full cascade (#3, 2026-07-01).
    """
    inflight = [s for s in started if s not in done]
    bits: list[str] = []
    if inflight:
        bits.append(f"in-flight step(s): {inflight}")
    if failures:
        bits.append(
            "captured step failure(s): " + "; ".join(f"{n} raised {t}: {m}" for n, t, m in failures)
        )
    return (" " + " | ".join(bits) + ".") if bits else ""


@dataclass(frozen=True, kw_only=True)
class ExecutionResult:
    run_id: UUID
    status: RunStatus  # COMPLETED or FAILED
    reason: str | None  # human-readable when FAILED; None on COMPLETED
    output_artifact_id: UUID | None  # None on FAILED


class LocalExecutor:
    """Local Tier-4 executor. Synchronous-looking API, async under the
    hood because nanobrain's ``Workflow.process`` is a coroutine."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        artifact_store: ArtifactStore,
        recorder: ProvenanceRecorder,
        workflow_base_dir: Path,
        actor: str = "local_executor",
        cascade_timeout_seconds: float = 600.0,
        cascade_settle_ms: int = 200,
        default_payload: dict[str, Any] | None = None,
        allow_empty_input: bool = False,
        allow_empty_output: bool = False,
        config_search_paths: list[str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._recorder = recorder
        self._workflow_base_dir = Path(workflow_base_dir).resolve()
        # Extra catalog roots threaded into Workflow.from_config so a composed
        # workflow that reuses wrappers from MULTIPLE catalog dirs resolves at
        # run time (nanobrain config_base Strategy 7). Roots come from
        # catalog_search_roots(composer_config). ROBUSTNESS: when the caller passes
        # None (didn't wire them), default to the canonical catalog roots so a
        # composed workflow referencing catalog components (e.g. entity_extraction.yml)
        # still resolves — a caller cannot silently forget the wiring. An explicit
        # empty list ([]) opts OUT (single-dir behavior). Strategy 7 is additive/
        # no-op-on-success, so the default roots never change non-catalog resolution.
        if config_search_paths is None:
            from apecx_integration.composition.component_catalog import catalog_search_roots

            config_search_paths = catalog_search_roots()
        self._config_search_paths = [str(Path(p).resolve()) for p in config_search_paths]
        self._actor = actor
        # G35 — cascade-drain knobs for ``Workflow.run``. The default
        # 600s/200ms pair is chosen to match the synonym-dictionary
        # bootstrap (long-running build) AND typical LLM-bound composed
        # workflows. Lower the timeout for pure-compute test runs; raise
        # it for workflows that legitimately exceed 10 minutes.
        self._cascade_timeout_seconds = cascade_timeout_seconds
        self._cascade_settle_ms = cascade_settle_ms
        # EMPTY-FAIL (2026-05-12): close the silent-failure shape where
        # the executor passed `{}` to ``workflow.run`` and treated the
        # trigger-init status dict as success. Empty input now FAILS
        # unless the caller opts in via ``allow_empty_input=True``.
        # Callers that need real input pass it via ``default_payload``.
        # Operators who intentionally test the empty-input branch
        # (e.g., the AC1 fixtures) set ``allow_empty_input=True``
        # AND document why in their test.
        self._default_payload: dict[str, Any] = default_payload or {}
        self._allow_empty_input: bool = allow_empty_input
        # EMPTY-OUTPUT (2026-05-12): symmetric gate on the OUTPUT
        # side. The RT-REAL integration test surfaced the shape
        # where workflow.run() returns
        # ``{"workflow_output": null, "status": "completed"}`` —
        # cascade drained cleanly, executor marked COMPLETED, but
        # the workflow's actual output was never populated. Tests
        # passing on this shape are false positives. Default-fail;
        # opt in via ``allow_empty_output=True`` for callers that
        # genuinely test the empty-output branch (fixture-driven
        # smoke tests where the workflow is intentionally inert).
        self._allow_empty_output: bool = allow_empty_output

    async def execute(self, run_id: UUID) -> ExecutionResult:
        yaml_path = self._validate_and_fetch(run_id)
        if yaml_path is None:
            # Three sub-cases, all returning yaml_path=None:
            # A) the run row doesn't exist (helper logged + bailed),
            # B) run exists with no workflow_config_id (helper
            #    committed FAILED via in_session mark),
            # C) workflow_config_id set but artifact / file missing
            #    (same — committed FAILED).
            #
            # Cluster AJ + AJ-followup (2026-04-26): be truthful.
            # Read actual DB status. If absent (case A), surface a
            # clear "run not found" reason rather than fabricating
            # FAILED for a row that doesn't exist. Otherwise, the
            # actual status IS FAILED (B or C) and we return that
            # with the operator-friendly "workflow_misconfigured"
            # reason.
            actual = self._read_actual_status(run_id)
            if actual is None:
                return ExecutionResult(
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    reason=(
                        f"run {run_id} not found in DB; cannot execute "
                        "a non-existent run. Caller should treat this "
                        "as a 404-equivalent rather than as a real "
                        "FAILED transition (no provenance event was "
                        "emitted)."
                    ),
                    output_artifact_id=None,
                )
            return ExecutionResult(
                run_id=run_id,
                status=actual,
                reason="workflow_misconfigured",
                output_artifact_id=None,
            )

        # Atomic claim. Two concurrent /workflows/execute calls on
        # the same run both pass _validate_and_fetch above (the helper
        # only validates state, doesn't claim). Without a guard here,
        # both proceed through workflow.process() to completion,
        # doubling every side effect. Migration 0002 added a partial
        # unique index on provenance_event(run_id) WHERE
        # event_type='RUN_STARTED'; the SECOND record() call here
        # raises IntegrityError, which we catch and short-circuit.
        # Found 2026-04-26 by adversarial test (cluster V3).
        from sqlalchemy.exc import IntegrityError

        try:
            self._recorder.record(
                run_id=run_id,
                event_type=ProvenanceEventType.RUN_STARTED,
                actor=self._actor,
                payload={
                    "workflow_yaml": str(yaml_path),
                    "workflow_base_dir": str(self._workflow_base_dir),
                },
            )
        except IntegrityError:
            log.warning(
                "Run %s already has a RUN_STARTED event; concurrent "
                "executor claimed it. Aborting without re-running.",
                run_id,
            )
            # Cluster AJ follow-up — we previously fabricated
            # status=RUNNING here on the assumption that the OTHER
            # executor was still running. Truth: the other executor
            # might already be COMPLETED, FAILED, or anywhere. Read
            # the actual DB status and surface it. The reason field
            # tells the caller WHY this executor didn't drive the
            # transition.
            return self._terminal_result(
                run_id=run_id,
                intended_status=RunStatus.RUNNING,
                transitioned=False,
                intended_reason="concurrent_executor_already_claimed_run",
                output_artifact_id=None,
            )

        import tempfile

        with tempfile.TemporaryDirectory(prefix="apecx_run_") as td:
            run_root = Path(td)
            staged_yaml = self._stage_workflow(yaml_path, run_root)

            try:
                # Lazy import — nanobrain is heavy; don't pay for it
                # in modules that only import LocalExecutor for typing.
                from nanobrain.core.workflow import Workflow

                workflow = Workflow.from_config(
                    str(staged_yaml), config_search_paths=self._config_search_paths
                )
            except Exception as exc:
                reason = f"workflow load failed: {type(exc).__name__}: {exc}"
                log.warning("Run %s: %s", run_id, reason)
                # C2 (2026-05-11): mirror the framework violation back
                # onto the composition record so the composer can be
                # held to account. Any violation that lands here is a
                # case the A1 compose-time validator did NOT catch —
                # that's a coverage gap worth measuring. The structured
                # record is stored on the GeneratedArtifact's
                # composition_summary JSON for later querying.
                self._record_runtime_violation(
                    run_id=run_id,
                    exc=exc,
                    failure_class="load_failed",
                )
                transitioned = self._mark_failed(run_id, reason, failure_class="load_failed")
                return self._terminal_result(
                    run_id=run_id,
                    intended_status=RunStatus.FAILED,
                    transitioned=transitioned,
                    intended_reason=reason,
                    output_artifact_id=None,
                )

            # EMPTY-FAIL gate: reject empty input unless explicitly
            # opted in. The earlier silent-failure shape was: pass
            # {} → workflow runs with no input → cascade fires
            # against empty data units → trigger-init dict gets
            # marked COMPLETED. Tests passed, product produced no
            # output. Now: empty + no opt-in = FAILED with a clear
            # reason that operators can act on.
            payload = self._default_payload
            if not payload and not self._allow_empty_input:
                reason = (
                    "executor refused to run with empty input. Pass "
                    "default_payload={...} when constructing LocalExecutor "
                    "OR set allow_empty_input=True to explicitly opt in. "
                    "Empty input previously masked silent failures: the "
                    "workflow loaded + the cascade fired but no real "
                    "data flowed; RUN_COMPLETED was misleading."
                )
                log.warning("Run %s: %s", run_id, reason)
                transitioned = self._mark_failed(
                    run_id, reason, failure_class="empty_input_refused"
                )
                return self._terminal_result(
                    run_id=run_id,
                    intended_status=RunStatus.FAILED,
                    transitioned=transitioned,
                    intended_reason=reason,
                    output_artifact_id=None,
                )

            # #3 (2026-07-01) — capture G37 step events during the run so a cascade TIMEOUT or a
            # swallowed step exception names the in-flight / failed step, not just "partial output
            # keys". Workflow.run(raise_on_cascade_timeout=False) returns status='cascade_timeout'
            # with the workflow outputs EMPTY; without these events the executor could only report
            # the missing output keys, never WHICH step hung or raised. Mirrors the capture pattern
            # in cli/_globus_data_transfer.py.
            from nanobrain.core.step_events import subscribe_to_step_events  # noqa: PLC0415

            _started: list[str] = []
            _done: set[str] = set()
            _step_failures: list[tuple[str, str, str]] = []

            def _capture_step(event: object) -> None:
                et = getattr(event, "event_type", None)
                name = getattr(event, "step_name", "?")
                if et == "step_start":
                    _started.append(name)
                elif et in ("step_complete", "step_failed"):
                    _done.add(name)
                    if et == "step_failed":
                        exc_info = (getattr(event, "payload", None) or {}).get("exception") or {}
                        _step_failures.append(
                            (name, exc_info.get("type", ""), exc_info.get("message", ""))
                        )

            def _step_diag() -> str:
                return _format_step_diagnostics(_started, _done, _step_failures)

            try:
                # G35 — adopt Workflow.run (G8) so the cascade is awaited
                # and workflow-level output data units are collected. Pre-G35
                # the executor called ``workflow.process({})`` and persisted
                # whatever process() returned in data-driven mode — which
                # is the load-bearing trigger-init status dict
                # ``{"status": "data_flow_initiated", ...}``, NOT the
                # workflow's actual outputs. Every multi-step composed
                # workflow that ran through this executor was silently
                # dropping its real outputs into the artifact's status
                # field while the cascade fired in the background and its
                # results vanished. Source: eval_03_nanobrain_gap_inventory.md
                # Round 4 G35 (2026-05-09).
                with subscribe_to_step_events(_capture_step):
                    raw_result = await workflow.run(
                        payload,
                        timeout=self._cascade_timeout_seconds,
                        settle_ms=self._cascade_settle_ms,
                        raise_on_cascade_timeout=False,
                    )
            except Exception as exc:
                reason = f"workflow execution failed: {type(exc).__name__}: {exc}{_step_diag()}"
                log.warning("Run %s: %s", run_id, reason)
                transitioned = self._mark_failed(run_id, reason, failure_class="execute_failed")
                return self._terminal_result(
                    run_id=run_id,
                    intended_status=RunStatus.FAILED,
                    transitioned=transitioned,
                    intended_reason=reason,
                    output_artifact_id=None,
                )

            # G35 — Workflow.run() can return a non-completed status
            # without raising. Treat ``cascade_timeout`` and
            # ``no_first_step`` as terminal failures rather than letting
            # them slip through to RUN_COMPLETED with a misleading payload.
            # The "fire-and-forget then claim COMPLETED" shape is exactly
            # the silent-failure class this executor exists to prevent.
            run_status = raw_result.get("status") if isinstance(raw_result, dict) else None
            if run_status in ("cascade_timeout", "no_first_step"):
                output_keys = (
                    [k for k in raw_result if not k.startswith("_") and k != "status"]
                    if isinstance(raw_result, dict)
                    else []
                )
                reason = (
                    f"workflow returned non-completed status="
                    f"{run_status!r}; cascade did not drain cleanly.{_step_diag()} "
                    f"Partial output keys: {output_keys}"
                )
                log.warning("Run %s: %s", run_id, reason)
                transitioned = self._mark_failed(run_id, reason, failure_class="execute_failed")
                return self._terminal_result(
                    run_id=run_id,
                    intended_status=RunStatus.FAILED,
                    transitioned=transitioned,
                    intended_reason=reason,
                    output_artifact_id=None,
                )

            # EMPTY-OUTPUT gate (2026-05-12): symmetric with the
            # EMPTY-FAIL input gate. If workflow.run() returned a
            # status=completed dict but every non-metadata output
            # value is None/empty, the cascade drained without
            # populating the workflow's outputs. Mark FAILED unless
            # the caller explicitly opted in.
            if not self._allow_empty_output and isinstance(raw_result, dict):
                meaningful_outputs = {
                    k: v
                    for k, v in raw_result.items()
                    if not k.startswith("_")
                    and k != "status"
                    and v is not None
                    and v != []
                    and v != {}
                    and v != ""
                }
                if not meaningful_outputs:
                    reason = (
                        "workflow returned no meaningful output. "
                        f"raw_result keys: {sorted(raw_result.keys())}, "
                        "all non-status values were None / empty. "
                        "Pass allow_empty_output=True to opt in, OR "
                        "investigate the workflow's link wiring + "
                        "trigger semantics (the cascade fired but no "
                        "data reached the workflow-level output)."
                    )
                    log.warning("Run %s: %s", run_id, reason)
                    transitioned = self._mark_failed(
                        run_id, reason, failure_class="empty_output_refused"
                    )
                    return self._terminal_result(
                        run_id=run_id,
                        intended_status=RunStatus.FAILED,
                        transitioned=transitioned,
                        intended_reason=reason,
                        output_artifact_id=None,
                    )

        # The workflow succeeded; persist the output artifact and
        # mark the run completed. If persistence fails (disk full,
        # FK violation, content-hash collision in some future schema,
        # etc.), the run was already past RUN_STARTED — we have to
        # transition it to a terminal state so it doesn't sit in
        # RUNNING until the sweeper catches it. Cluster AJ fail-fast
        # extension (2026-04-26).
        try:
            output_artifact_id = self._persist_output(run_id, raw_result)
        except Exception as exc:
            reason = (
                f"workflow succeeded but output persistence failed: {type(exc).__name__}: {exc}"
            )
            log.warning("Run %s: %s", run_id, reason)
            transitioned = self._mark_failed(run_id, reason, failure_class="persist_failed")
            return self._terminal_result(
                run_id=run_id,
                intended_status=RunStatus.FAILED,
                transitioned=transitioned,
                intended_reason=reason,
                output_artifact_id=None,
            )

        transitioned = self._mark_completed(run_id, output_artifact_id)

        return self._terminal_result(
            run_id=run_id,
            intended_status=RunStatus.COMPLETED,
            transitioned=transitioned,
            intended_reason=None,
            output_artifact_id=output_artifact_id,
        )

    def _terminal_result(
        self,
        *,
        run_id: UUID,
        intended_status: RunStatus,
        transitioned: bool,
        intended_reason: str | None,
        output_artifact_id: UUID | None,
    ) -> ExecutionResult:
        """Construct the ExecutionResult, honoring the ACTUAL DB state.

        Cluster AJ (2026-04-26) — fail-fast: the executor must NOT
        report a terminal status it didn't actually achieve. If
        ``_mark_completed`` / ``_mark_failed`` returned False, the
        run had already left an active state (likely swept to
        FAILED, or externally CANCELLED), and the actual DB
        status — not the executor's attempted one — is the truth.
        Surface a non-None ``reason`` so the caller / HTTP route /
        MCP client knows the executor didn't drive this transition.
        """
        if transitioned:
            return ExecutionResult(
                run_id=run_id,
                status=intended_status,
                reason=intended_reason,
                output_artifact_id=output_artifact_id,
            )
        actual = self._read_actual_status(run_id)
        if actual is None:
            # Run row vanished. That's an upstream invariant
            # violation (Run rows are append-only after insert);
            # surface as FAILED with a clear reason so the caller
            # has a terminal status to act on.
            return ExecutionResult(
                run_id=run_id,
                status=RunStatus.FAILED,
                reason=(
                    f"executor finished but run {run_id} no longer "
                    "exists in the DB; somebody deleted it mid-flight"
                ),
                output_artifact_id=output_artifact_id,
            )
        return ExecutionResult(
            run_id=run_id,
            status=actual,
            reason=(
                f"executor attempted {intended_status.value} but the "
                f"run was already in status={actual.value} — another "
                "writer (sweeper, /workflows/cancel, etc) owned the "
                "terminal transition; executor did NOT emit a "
                "terminal provenance event"
            ),
            output_artifact_id=output_artifact_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_and_fetch(self, run_id: UUID) -> Path | None:
        with self._session_factory() as session:
            run = session.get(RunORM, run_id)
            if run is None:
                log.warning("Run %s not found; skipping execution", run_id)
                return None
            if run.workflow_config_id is None:
                self._mark_failed(
                    run_id,
                    "run has no workflow_config_id",
                    failure_class="workflow_misconfigured",
                    in_session=session,
                )
                session.commit()
                return None
            artifact = session.get(ArtifactORM, run.workflow_config_id)
            if artifact is None:
                self._mark_failed(
                    run_id,
                    "workflow artifact row missing",
                    failure_class="workflow_misconfigured",
                    in_session=session,
                )
                session.commit()
                return None
            on_disk = Path(artifact.location)
            if not on_disk.is_file():
                self._mark_failed(
                    run_id,
                    f"workflow YAML not found on disk: {on_disk}",
                    failure_class="workflow_misconfigured",
                    in_session=session,
                )
                session.commit()
                return None
            return on_disk

    def _stage_workflow(self, yaml_path: Path, run_root: Path) -> Path:
        """Build a staging directory that nanobrain can load from.

        The composed YAML references ``steps/*.yml`` relative paths. We symlink the configured
        ``workflow_base_dir/steps`` into the staging dir and copy the YAML in, so relative resolution
        works without mutating the source tree.

        NOVEL STEPS (#1c): when the composed YAML carries ``_apecx_sandboxed_novel_config`` (a
        SandboxedNovelStep routed by the spec expander), each novel step needs its own
        ``steps/<id>.yml`` file-path config (a BaseStep can't take inline config — G121). We can't add
        files to a symlink pointing at the read-only catalog, so we build a REAL ``steps/`` dir (copy
        the catalog steps in, then write the novel per-step YAMLs), and STRIP the metadata key before
        writing the staged workflow so ``Workflow.from_config`` never sees it.
        """
        steps_src = self._workflow_base_dir / "steps"

        import yaml as _yaml

        doc = _yaml.safe_load(yaml_path.read_text())
        novel_cfg = (
            doc.pop("_apecx_sandboxed_novel_config", None) if isinstance(doc, dict) else None
        )

        if novel_cfg:
            import shutil

            steps_dir = run_root / "steps"
            steps_dir.mkdir(exist_ok=True)
            if steps_src.is_dir():
                for f in steps_src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, steps_dir / f.name)
            steps_dir_resolved = steps_dir.resolve()
            for step_id, cfg in novel_cfg.items():
                dest = (steps_dir / f"{step_id}.yml").resolve()
                # Defense-in-depth (WorkflowStepSpec.id already restricts the charset): NEVER write
                # outside the staged steps/ dir, even if a traversal id somehow reached here — writing
                # an attacker-controlled YAML (it embeds novel_source) outside run_root, or clobbering
                # a trusted catalog step, is a host-write escape (#1c review-gate blocker).
                if dest.parent != steps_dir_resolved:
                    raise ValueError(
                        f"refusing to stage novel-step config outside steps/: unsafe step id {step_id!r}"
                    )
                dest.write_text(_yaml.safe_dump(cfg, sort_keys=False))
            staged = run_root / "workflow.yml"
            staged.write_text(_yaml.safe_dump(doc, sort_keys=False))
            return staged

        if steps_src.is_dir():
            (run_root / "steps").symlink_to(steps_src, target_is_directory=True)
        staged = run_root / "workflow.yml"
        staged.write_bytes(yaml_path.read_bytes())
        return staged

    def _persist_output(self, run_id: UUID, raw_result: object) -> UUID:
        payload_bytes = json.dumps(raw_result, default=str, indent=2).encode("utf-8")
        artifact = self._artifact_store.store(
            content=payload_bytes,
            kind=ArtifactKind.OUTPUT,
            run_id=run_id,
            mime_type="application/json",
        )
        return artifact.id

    # Conditional terminal-transition helpers (cluster AB,
    # 2026-04-26). The executor must not "rebirth" a run from a
    # terminal state. If the sweeper already marked the run FAILED
    # (cluster Z), or an external CANCELLED arrives, or any other
    # terminal transition has happened first, the executor's
    # _mark_completed / _mark_failed must be a no-op for the row
    # AND must skip emitting the terminal provenance event (the
    # winner already emitted theirs).
    #
    # The "legal source states" differ between the two helpers:
    # _mark_completed implies the executor actually ran the
    # workflow, so status must have been RUNNING/PAUSED. PENDING
    # → COMPLETED would be a bug (executor never started).
    # _mark_failed includes PENDING because validation failures
    # (no workflow_config_id, artifact missing on disk) fire
    # before the RUN_STARTED transition — we want those PENDING
    # runs marked FAILED so they don't sit as orphans.
    _COMPLETED_SOURCE_STATES = (RunStatus.RUNNING, RunStatus.PAUSED)
    _FAILED_SOURCE_STATES = (
        RunStatus.PENDING,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
    )

    def _mark_completed(self, run_id: UUID, output_artifact_id: UUID) -> bool:
        """Transition run to COMPLETED + emit RUN_COMPLETED.

        Returns True if THIS call performed the transition (the
        caller is the source of truth — it can return COMPLETED).
        Returns False if the run had already left RUNNING/PAUSED
        before our UPDATE — the caller is NOT the source of truth
        and must NOT pretend it completed the run. Cluster AJ
        (2026-04-26) added the bool to fix the silent-success
        path where execute() returned status=COMPLETED even
        though the helper had skipped the transition.
        """
        now = datetime.now(UTC)
        with self._session_factory() as session:
            result = session.execute(
                update(RunORM)
                .where(RunORM.id == run_id)
                .where(RunORM.status.in_(self._COMPLETED_SOURCE_STATES))
                .values(status=RunStatus.COMPLETED, completed_at=now)
            )
            session.commit()
            if result.rowcount == 0:
                log.warning(
                    "Run %s not in RUNNING/PAUSED at _mark_completed; "
                    "skipping terminal transition + RUN_COMPLETED event",
                    run_id,
                )
                return False
        self._recorder.record(
            run_id=run_id,
            event_type=ProvenanceEventType.RUN_COMPLETED,
            actor=self._actor,
            payload={"output_artifact_id": str(output_artifact_id)},
        )
        return True

    def _mark_failed(
        self,
        run_id: UUID,
        reason: str,
        *,
        failure_class: str,
        in_session: Session | None = None,
    ) -> bool:
        """Transition run to FAILED + emit RUN_FAILED.

        Returns True if THIS call performed the transition. Returns
        False if the run had already left an active state (already
        terminal, or absent). Cluster AJ (2026-04-26) — see
        ``_mark_completed`` rationale.

        For the in_session pre-RUN_STARTED validation-failure path,
        we keep the ORM-tracked in-memory mutation (race-free
        because no other writer touches PENDING-without-an-
        artifact) but still surface the bool truthfully: True only
        if the run row was found AND was in an eligible source
        state.
        """
        now = datetime.now(UTC)

        if in_session is not None:
            # Pre-RUN_STARTED validation-failure path. The run is
            # PENDING and no other writer touches it yet, so race
            # protection isn't relevant. Use the ORM-tracked
            # in-memory mutation so we don't issue a DML statement
            # that holds the SQLite writer lock and blocks the
            # recorder's separate session from committing the
            # RUN_FAILED event a few lines below. Caller commits.
            run = in_session.get(RunORM, run_id)
            if run is None:
                log.warning(
                    "Run %s not found at in-session _mark_failed; "
                    "skipping terminal transition + RUN_FAILED event",
                    run_id,
                )
                return False
            if run.status not in self._FAILED_SOURCE_STATES:
                log.warning(
                    "Run %s already terminal (status=%s) at in-session "
                    "_mark_failed; skipping terminal transition + "
                    "RUN_FAILED event",
                    run_id,
                    run.status,
                )
                return False
            run.status = RunStatus.FAILED
            run.completed_at = now
        else:
            # Post-RUN_STARTED failure path. The run was claimed
            # via the migration-0002 partial unique index, so its
            # status was RUNNING/PAUSED — until possibly the
            # sweeper or an external CANCELLED arrived. Conditional
            # UPDATE so we don't rebirth a terminal run.
            with self._session_factory() as session:
                result = session.execute(
                    update(RunORM)
                    .where(RunORM.id == run_id)
                    .where(RunORM.status.in_(self._FAILED_SOURCE_STATES))
                    .values(status=RunStatus.FAILED, completed_at=now)
                )
                session.commit()
                if result.rowcount == 0:
                    log.warning(
                        "Run %s already terminal (or absent) at "
                        "_mark_failed; skipping terminal transition + "
                        "RUN_FAILED event",
                        run_id,
                    )
                    return False
        self._recorder.record(
            run_id=run_id,
            event_type=ProvenanceEventType.RUN_FAILED,
            actor=self._actor,
            payload={"reason": reason, "failure_class": failure_class},
        )
        return True

    def _read_actual_status(self, run_id: UUID) -> RunStatus | None:
        """Fetch the run's current status from the DB. Used by
        ``execute()`` when ``_mark_completed`` / ``_mark_failed``
        return False — i.e. somebody else owns the terminal
        transition for this run, and we must report THEIR truth
        rather than our (rejected) attempted transition.
        """
        with self._session_factory() as session:
            run = session.get(RunORM, run_id)
            return run.status if run is not None else None

    # ------------------------------------------------------------------
    # C2 — runtime-violation feedback channel
    # ------------------------------------------------------------------

    # Heuristic markers that classify a runtime exception's text into
    # a rule_id that mirrors A1's vocabulary. Frozen at module level
    # so a future framework re-wording surfaces here as a missed
    # match (the regression metric would silently flatline otherwise).
    _RUNTIME_VIOLATION_MARKERS: tuple[tuple[str, str], ...] = (
        (
            "FRAMEWORK VIOLATION: Inline dict configuration not supported",
            "step_inline_config_forbidden",
        ),
        (
            "FRAMEWORK VIOLATION",
            "framework_violation_unclassified",
        ),
        (
            "FAILED TO INSTANTIATE",
            "from_config_failed",
        ),
        (
            "ComponentConfigurationError",
            "component_configuration_error",
        ),
        (
            "ModuleNotFoundError",
            "module_not_found",
        ),
        (
            "AttributeError",
            "attribute_error",
        ),
    )

    @classmethod
    def _classify_runtime_violation(cls, exc: BaseException) -> str:
        """Map an exception's text onto an A1-style rule_id.

        Class-bound so unit tests can call it without instantiating
        a full executor (which requires a session_factory + recorder).
        """
        text = f"{type(exc).__name__}: {exc!s}"
        for marker, rule_id in cls._RUNTIME_VIOLATION_MARKERS:
            if marker in text:
                return rule_id
        return "runtime_other"

    def _record_runtime_violation(
        self,
        *,
        run_id: UUID,
        exc: BaseException,
        failure_class: str,
    ) -> None:
        """Persist a structured runtime violation onto the run's
        GeneratedArtifact.composition_summary JSON.

        This is the C2 feedback channel: every exception that lands
        in the executor's load_failed branch is a case A1 did NOT
        catch at compose-time. Operators / regression queries:

            SELECT
              ga.artifact_id,
              ga.composition_summary->>'runtime_violations' AS rv
            FROM generated_artifact ga
            WHERE ga.composition_summary->>'runtime_violations' IS NOT NULL;

        The persisted record carries the rule_id, the failure_class,
        the truncated exception text, and a UTC timestamp.

        Persistence failures here MUST NOT mask the original error.
        We swallow exceptions from the DB path so the caller's
        ``_mark_failed`` / ``_terminal_result`` continues to run.
        """
        from datetime import UTC, datetime

        from sqlalchemy.orm.attributes import flag_modified

        rule_id = self._classify_runtime_violation(exc)
        record = {
            "rule_id": rule_id,
            "failure_class": failure_class,
            "exception_type": type(exc).__name__,
            "exception_message": (str(exc)[:2000]),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        try:
            with self._session_factory() as session:
                run = session.get(RunORM, run_id)
                if run is None or run.workflow_config_id is None:
                    log.debug(
                        "Run %s has no workflow_config_id; cannot record runtime violation %s",
                        run_id,
                        rule_id,
                    )
                    return
                # Lazy import to avoid pulling GeneratedArtifactORM
                # into the typing-only import path.
                from apecx_integration.control_plane.models.entities import (
                    GeneratedArtifact as GeneratedArtifactORM,
                )

                ga = session.get(GeneratedArtifactORM, run.workflow_config_id)
                if ga is None:
                    log.debug(
                        "GeneratedArtifact missing for run %s; cannot record runtime violation %s",
                        run_id,
                        rule_id,
                    )
                    return
                summary = dict(ga.composition_summary or {})
                violations = list(summary.get("runtime_violations") or [])
                violations.append(record)
                summary["runtime_violations"] = violations
                ga.composition_summary = summary
                # JSON columns are mutated in place; SQLAlchemy needs
                # a flag to know it changed. Without this, the
                # commit() below silently no-ops on the JSON.
                flag_modified(ga, "composition_summary")
                session.commit()
                log.info(
                    "Recorded runtime violation %s on artifact %s for run %s",
                    rule_id,
                    run.workflow_config_id,
                    run_id,
                )
        except Exception as persist_exc:  # don't mask the original failure
            log.warning(
                "Failed to persist runtime violation for run %s (rule_id=%s): %s: %s",
                run_id,
                rule_id,
                type(persist_exc).__name__,
                persist_exc,
            )


def run_sync(executor: LocalExecutor, run_id: UUID) -> ExecutionResult:
    """Sync wrapper for callers outside an async context (e.g. CLI,
    tests that don't want to spin up an event loop manually)."""
    return asyncio.run(executor.execute(run_id))


__all__ = ["ExecutionResult", "LocalExecutor", "run_sync"]
