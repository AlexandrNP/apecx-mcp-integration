"""C2 — LocalExecutor → GeneratedArtifact runtime-violation persistence.

When ``Workflow.from_config()`` raises in the executor's load_failed
branch, the structured violation is now written back onto the
GeneratedArtifact's composition_summary JSON. This is the feedback
channel that closes the loop on A1's coverage — any violation that
lands here is a case A1 didn't catch at compose-time.

Test strategy:

  1. Build a real migrated SQLite DB + a real ArtifactStore.
  2. Hand-craft a workflow YAML where ``Workflow.from_config`` will
     fail (a step config path that points at a non-existent wrapper).
     A1's validator does NOT check disk existence for catalog paths,
     so this slips through compose() without raising — exactly the
     coverage gap C2 is designed to measure.
  3. Drive ``executor.execute(run_id)``.
  4. Assert the Run is FAILED AND the GeneratedArtifact's
     composition_summary['runtime_violations'] is populated with the
     expected rule_id + truncated message.

No mocks for the persistence path — the test runs against a real
DB + a real on-disk artifact root, per the workspace mocks-carve-out.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

try:
    import nanobrain.core.workflow  # noqa: F401

    _NANOBRAIN_AVAILABLE = True
except ImportError:
    _NANOBRAIN_AVAILABLE = False

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
from apecx_integration.control_plane.models.entities import (
    GeneratedArtifact as GeneratedArtifactORM,
)
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import (
    ArtifactKind,
    RunStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _NANOBRAIN_AVAILABLE,
        reason=(
            "nanobrain not importable — run under the project venv "
            "(.venv/bin/python -m pytest ...), not system Python"
        ),
    ),
]


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


def _seed_run(engine, run_id: UUID) -> None:
    """A run row in RUNNING — bypasses the start_workflow route and
    drops us directly into the executor's load_failed branch."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, "
                "started_at) "
                "VALUES (:id, 'c2_test', 'RUNNING', :ts, :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )


def test_c2_runtime_violation_persisted_to_generated_artifact(tmp_path):
    """End-to-end persistence: executor load failure → structured
    violation on GeneratedArtifact.composition_summary['runtime_violations'].
    """
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)

    artifact_root = tmp_path / "artifact_root"
    store = ArtifactStore(session_factory, recorder, root=artifact_root)

    run_id = uuid4()
    _seed_run(engine, run_id)

    # YAML that A1 would let through (no inline-dict, real class
    # path, etc.) but Workflow.from_config will reject because the
    # referenced wrapper YAML doesn't exist on disk.
    bad_yaml = textwrap.dedent(
        """\
        name: c2_runtime_violation_workflow
        description: "config path points at a non-existent wrapper"
        version: "0.1.0"
        steps:
          rag_synth:
            class: "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
            config: "steps/this_path_does_not_exist.yml"
        links: {}
        """
    )

    generated_metadata = GenerationMetadata(
        source_prompt="seed for C2 runtime-violation test",
        library_version="test-1.0",
        llm_model="placeholder",
        llm_model_version_hash="0" * 64,
        composition_summary={
            "steps_reused": 1,
            "steps_generated": 0,
            "steps_swapped": 0,
            "summary_sentence": "test",
            "step_categorizations": [],
            "review_notes": [],
            "novel_python_by_step": {},
            "compose_retries": 0,
        },
    )
    artifact = store.store(
        content=bad_yaml.encode("utf-8"),
        kind=ArtifactKind.GENERATED_WORKFLOW,
        run_id=run_id,
        mime_type="application/yaml",
        generated_metadata=generated_metadata,
    )

    # Backlink the run -> artifact so the executor will load it.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact.id), "rid": str(run_id)},
        )

    workflow_base_dir = (
        REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "rag_e2e_synthesis"
    )
    executor = LocalExecutor(
        session_factory=session_factory,
        recorder=recorder,
        artifact_store=store,
        workflow_base_dir=workflow_base_dir,
        actor="c2_test",
    )
    import asyncio

    result = asyncio.run(executor.execute(run_id))

    # The execute() call must surface as FAILED with a load_failed
    # reason.
    assert result.status is RunStatus.FAILED, result
    assert result.reason is not None and "workflow load failed" in result.reason

    # And the structured violation must be persisted onto the
    # GeneratedArtifact's composition_summary.
    with session_factory() as session:
        ga = session.get(GeneratedArtifactORM, artifact.id)
        assert ga is not None
        rv = (ga.composition_summary or {}).get("runtime_violations")
        assert rv, (
            "executor load_failed but no runtime_violations recorded "
            f"on GeneratedArtifact; composition_summary={ga.composition_summary!r}"
        )
        assert isinstance(rv, list) and len(rv) == 1
        entry = rv[0]
        assert "rule_id" in entry
        assert entry["failure_class"] == "load_failed"
        assert "exception_type" in entry
        assert "exception_message" in entry
        assert "recorded_at" in entry
