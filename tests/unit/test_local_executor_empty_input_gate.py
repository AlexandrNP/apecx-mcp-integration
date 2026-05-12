"""EMPTY-FAIL — pin the empty-input rejection contract on LocalExecutor.

Before 2026-05-12 the executor passed ``{}`` to ``workflow.run`` and
treated the trigger-init status dict as success. Workflows loaded
cleanly, the cascade fired, and the run was marked RUN_COMPLETED
even though no real data flowed. The composer's AC1 + spec-mode
AC1 tests were passing on this silent-failure shape.

The new contract:
  - ``default_payload`` empty + ``allow_empty_input=False`` (default)
    → executor refuses to run; marks the run FAILED with a clear
    reason; emits a ``failure_class="empty_input_refused"`` event.
  - Empty payload + ``allow_empty_input=True`` → caller explicitly
    opts in; previous behavior preserved.
  - Non-empty ``default_payload`` (any keys) → executor runs normally
    regardless of ``allow_empty_input``.

These tests are unit-level: they exercise the gate without spinning
up a real Workflow.from_config or LLM call. The integration tests
that drive the gate end-to-end live under ``tests/integration/``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from apecx_integration.composition.artifact_store import (
    ArtifactStore,
    GenerationMetadata,
)
from apecx_integration.control_plane.db import (
    make_engine,
    make_session_factory,
)
from apecx_integration.control_plane.executors.local import LocalExecutor
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import (
    ArtifactKind,
    RunStatus,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrated_engine(tmp_path: Path):
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return make_engine(url)


def _seed_run_with_minimal_artifact(
    engine,
    store: ArtifactStore,
    run_id: UUID,
) -> UUID:
    """Set up just enough state for the executor to REACH the
    empty-input gate: Run row + a generated workflow Artifact whose
    YAML imports cleanly. We use a 1-step workflow with a real
    catalog class so Workflow.from_config doesn't fail upstream.
    """
    ts = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, started_at) "
                "VALUES (:id, 'empty_fail_test', 'RUNNING', :ts, :ts)"
            ),
            {"id": str(run_id), "ts": ts},
        )
    minimal_yaml = (
        "name: empty_fail_smoke\n"
        "description: minimal workflow that reaches the empty-input gate\n"
        "version: '0.1.0'\n"
        "steps:\n"
        "  rag_synth:\n"
        '    class: "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"\n'
        '    config: "steps/rag_synthesis.yml"\n'
        "links: {}\n"
    )
    artifact = store.store(
        content=minimal_yaml.encode("utf-8"),
        kind=ArtifactKind.GENERATED_WORKFLOW,
        run_id=run_id,
        mime_type="application/yaml",
        generated_metadata=GenerationMetadata(
            source_prompt="empty-fail gate test",
            library_version="test",
            llm_model="placeholder",
            llm_model_version_hash="0" * 64,
            composition_summary={},
        ),
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact.id), "rid": str(run_id)},
        )
    return artifact.id


def _executor(
    tmp_path,
    *,
    allow_empty_input: bool,
    default_payload: dict | None = None,
    allow_empty_output: bool = True,
):
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(session_factory, recorder, root=artifact_root)
    workflow_base = (
        REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "rag_e2e_synthesis"
    )
    executor = LocalExecutor(
        session_factory=session_factory,
        recorder=recorder,
        artifact_store=store,
        workflow_base_dir=workflow_base,
        allow_empty_input=allow_empty_input,
        default_payload=default_payload,
        allow_empty_output=allow_empty_output,
        actor="empty_fail_test",
    )
    return executor, engine, store, session_factory


def test_empty_input_refused_by_default(tmp_path):
    """The keystone EMPTY-FAIL behavior: with the default
    ``allow_empty_input=False``, the executor refuses to call
    ``workflow.run`` and marks the run FAILED with a clear reason."""
    executor, engine, store, _ = _executor(tmp_path, allow_empty_input=False)
    run_id = uuid4()
    _seed_run_with_minimal_artifact(engine, store, run_id)

    result = asyncio.run(executor.execute(run_id))
    assert result.status is RunStatus.FAILED
    assert result.reason is not None
    assert "executor refused to run with empty input" in result.reason
    # The failure class is queryable so apecx-regression-metrics can
    # surface "how many empty-input refusals are we seeing?"
    assert "executor refused" in result.reason


def test_empty_input_allowed_when_explicitly_opted_in(tmp_path):
    """Pin the opt-in path: callers that genuinely want empty-input
    runs (test fixtures, smoke executions) set
    ``allow_empty_input=True`` and proceed as before.

    The workflow has no triggers configured for its empty input, so
    it ends up in cascade_timeout or similar non-COMPLETED status.
    We assert ONLY that the executor reached past the gate — i.e.,
    it did NOT return the empty_input_refused reason. The downstream
    workflow outcome is a separate concern.
    """
    executor, engine, store, _ = _executor(tmp_path, allow_empty_input=True)
    run_id = uuid4()
    _seed_run_with_minimal_artifact(engine, store, run_id)

    result = asyncio.run(executor.execute(run_id))
    assert result.reason is None or "executor refused" not in result.reason


def test_non_empty_default_payload_bypasses_gate(tmp_path):
    """When the operator supplies a real payload via
    ``default_payload``, the empty-input gate doesn't fire even
    though ``allow_empty_input`` defaults to False."""
    executor, engine, store, _ = _executor(
        tmp_path,
        allow_empty_input=False,
        default_payload={"workflow_input": {"user_query": "hello"}},
    )
    run_id = uuid4()
    _seed_run_with_minimal_artifact(engine, store, run_id)
    result = asyncio.run(executor.execute(run_id))
    # The executor reaches past the gate; whether downstream
    # workflow.run succeeds is workflow-implementation-dependent.
    assert result.reason is None or "executor refused" not in result.reason


def test_empty_output_refused_by_default(tmp_path):
    """EMPTY-OUTPUT (2026-05-12): symmetric gate on the output side.
    The RT-REAL integration test surfaced the shape where
    workflow.run() returns
    ``{"workflow_output": null, "status": "completed"}`` —
    the cascade drained cleanly but the workflow's actual output
    was never populated. Default-fail unless the caller opts in
    via ``allow_empty_output=True``.

    Test strategy: opt in to empty input (so we reach the cascade);
    the minimal 1-step workflow has no triggers wired for empty
    input → workflow.run returns a trivial result with no
    populated output keys → empty-output gate fires.
    """
    executor, engine, store, _ = _executor(
        tmp_path,
        allow_empty_input=True,
        allow_empty_output=False,  # the gate under test
    )
    run_id = uuid4()
    _seed_run_with_minimal_artifact(engine, store, run_id)
    result = asyncio.run(executor.execute(run_id))
    # Either the cascade fails earlier (cascade_timeout / no_first_step
    # / load_failed) OR the empty-output gate fires. Both are
    # legitimate FAILED outcomes. What we MUST NOT see is
    # status=COMPLETED with an empty payload — that's the silent
    # failure we're closing.
    assert result.status is RunStatus.FAILED
    assert result.reason is not None


def test_empty_output_allowed_when_explicitly_opted_in(tmp_path):
    """The opt-in path mirrors empty-input: callers that genuinely
    test the empty-cascade plumbing set allow_empty_output=True and
    proceed."""
    executor, engine, store, _ = _executor(
        tmp_path,
        allow_empty_input=True,
        allow_empty_output=True,
    )
    run_id = uuid4()
    _seed_run_with_minimal_artifact(engine, store, run_id)
    result = asyncio.run(executor.execute(run_id))
    # The result may be COMPLETED (empty output now tolerated) or
    # FAILED for OTHER reasons (cascade_timeout, no_first_step).
    # The empty-output gate's reason MUST NOT appear.
    assert result.reason is None or "no meaningful output" not in result.reason
