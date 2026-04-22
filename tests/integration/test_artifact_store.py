"""T11: ArtifactStore against a real migrated SQLite DB (AC1, AC3, AC4).

No mocks. Session factory + ProvenanceRecorder + disk storage all real.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from apecx_integration.composition.artifact_store import (
    ArtifactNotFound,
    ArtifactStore,
    GenerationMetadata,
)
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import ArtifactKind

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _seeded_engine_and_run(tmp_path):
    """Fresh migrated SQLite + one Run row so FK references are satisfied."""
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")

    engine = make_engine(url)
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )
    return engine, run_id


@pytest.fixture
def store(tmp_path):
    engine, run_id = _seeded_engine_and_run(tmp_path)
    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)
    artifact_root = tmp_path / "artifact_root"
    store = ArtifactStore(session_factory, recorder, root=artifact_root)
    return store, engine, run_id


def _gen_meta(**overrides) -> GenerationMetadata:
    defaults = {
        "source_prompt": "generate a workflow",
        "library_version": "1.2.3",
        "llm_model": "claude-opus-4-7",
        "llm_model_version_hash": "a" * 64,
        "composition_summary": {"steps_reused": 3, "steps_generated": 1},
    }
    defaults.update(overrides)
    return GenerationMetadata(**defaults)


def test_store_generated_workflow_persists_row_and_file(store) -> None:
    st, engine, run_id = store
    content = b"nanobrain: {}\nsteps: []\n"
    artifact = st.store(
        content=content,
        kind=ArtifactKind.GENERATED_WORKFLOW,
        run_id=run_id,
        mime_type="application/yaml",
        generated_metadata=_gen_meta(),
    )
    assert Path(artifact.location).read_bytes() == content
    assert len(artifact.content_hash) == 64
    # GeneratedArtifact sidecar row landed too.
    with engine.connect() as conn:
        ga = conn.execute(
            text(
                "SELECT llm_model, library_version FROM generated_artifact "
                "WHERE artifact_id = :id"
            ),
            {"id": str(artifact.id)},
        ).one()
    assert ga[0] == "claude-opus-4-7"
    assert ga[1] == "1.2.3"


def test_ac1_two_generations_of_same_content_produce_two_distinct_rows(store) -> None:
    """AP §5.11 AC1: even with identical content (temperature=0 edge case),
    two generation events must produce two distinct GeneratedArtifact rows
    so the audit trail reflects two separate generation events.
    """
    st, engine, run_id = store
    content = b"identical-bytes"
    a1 = st.store(
        content=content, kind=ArtifactKind.GENERATED_WORKFLOW, run_id=run_id,
        mime_type="application/yaml", generated_metadata=_gen_meta(),
    )
    a2 = st.store(
        content=content, kind=ArtifactKind.GENERATED_WORKFLOW, run_id=run_id,
        mime_type="application/yaml", generated_metadata=_gen_meta(),
    )
    assert a1.id != a2.id
    assert a1.content_hash == a2.content_hash  # identical content → same hash


def test_ac3_load_content_raises_when_file_manually_deleted(store) -> None:
    st, _engine, run_id = store
    artifact = st.store(
        content=b"irrelevant", kind=ArtifactKind.GENERATED_WORKFLOW,
        run_id=run_id, mime_type="application/yaml",
        generated_metadata=_gen_meta(),
    )
    Path(artifact.location).unlink()
    with pytest.raises(ArtifactNotFound, match="file .* is gone"):
        st.load_content(artifact.id)


def test_load_content_detects_bit_tampering(store) -> None:
    """Someone corrupts the on-disk file in place; load_content must refuse
    to return bytes that disagree with the stored hash.
    """
    st, _engine, run_id = store
    artifact = st.store(
        content=b"honest content", kind=ArtifactKind.GENERATED_WORKFLOW,
        run_id=run_id, mime_type="application/yaml",
        generated_metadata=_gen_meta(),
    )
    Path(artifact.location).write_bytes(b"tampered content")
    with pytest.raises(ValueError, match="content_hash mismatch"):
        st.load_content(artifact.id)


def test_input_artifact_rejects_generated_metadata(store) -> None:
    st, _engine, run_id = store
    with pytest.raises(ValueError, match="does not accept generated_metadata"):
        st.store(
            content=b"x", kind=ArtifactKind.INPUT, run_id=run_id,
            mime_type="text/plain", generated_metadata=_gen_meta(),
        )


def test_generated_kind_requires_metadata(store) -> None:
    st, _engine, run_id = store
    with pytest.raises(ValueError, match="requires generated_metadata"):
        st.store(
            content=b"x", kind=ArtifactKind.GENERATED_WORKFLOW, run_id=run_id,
            mime_type="application/yaml",
        )


def test_generated_artifact_emits_workflow_generated_provenance(store) -> None:
    st, engine, run_id = store
    st.store(
        content=b"y", kind=ArtifactKind.GENERATED_PYTHON, run_id=run_id,
        mime_type="text/x-python", generated_metadata=_gen_meta(),
    )
    with engine.connect() as conn:
        kinds = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT event_type FROM provenance_event "
                    "WHERE run_id = :rid ORDER BY timestamp"
                ),
                {"rid": str(run_id)},
            ).all()
        ]
    assert kinds == ["WORKFLOW_GENERATED"]


def test_non_generated_artifact_does_not_emit_provenance(store) -> None:
    """INPUT / INTERMEDIATE / OUTPUT artifacts are regular data, not the
    subject of the WORKFLOW_GENERATED event. Keeps the hash chain free
    of low-signal entries.
    """
    st, engine, run_id = store
    st.store(
        content=b"z", kind=ArtifactKind.INPUT, run_id=run_id,
        mime_type="text/plain",
    )
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM provenance_event WHERE run_id = :rid"
            ),
            {"rid": str(run_id)},
        ).scalar()
    assert count == 0


def test_ac4_git_commit_happens_when_env_set(store, tmp_path, monkeypatch) -> None:
    """Point GENERATED_ARTIFACTS_REPO_PATH at a real git repo and verify
    the artifact lands as a commit.
    """
    st, _engine, run_id = store
    repo = tmp_path / "gen_artifacts"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "test"], check=True,
    )
    monkeypatch.setenv("GENERATED_ARTIFACTS_REPO_PATH", str(repo))

    artifact = st.store(
        content=b"workflow: {}\n", kind=ArtifactKind.GENERATED_WORKFLOW,
        run_id=run_id, mime_type="application/yaml",
        generated_metadata=_gen_meta(),
    )
    # File landed in the repo and was committed.
    assert (repo / f"{artifact.id}.yml").is_file()
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        check=True, capture_output=True, text=True,
    )
    assert str(artifact.id)[:8] in log.stdout or "apecx:" in log.stdout


def test_ac4_no_git_env_no_git_commit(store, tmp_path, monkeypatch) -> None:
    """Without the env var set, store() does not call git at all."""
    st, _engine, run_id = store
    monkeypatch.delenv("GENERATED_ARTIFACTS_REPO_PATH", raising=False)
    # Should not raise. No git infra around.
    st.store(
        content=b"w", kind=ArtifactKind.GENERATED_WORKFLOW, run_id=run_id,
        mime_type="application/yaml", generated_metadata=_gen_meta(),
    )


def test_git_env_pointing_at_non_repo_raises(store, tmp_path, monkeypatch) -> None:
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    monkeypatch.setenv("GENERATED_ARTIFACTS_REPO_PATH", str(not_a_repo))
    st, _engine, run_id = store
    with pytest.raises(RuntimeError, match="not a git working tree"):
        st.store(
            content=b"x", kind=ArtifactKind.GENERATED_WORKFLOW, run_id=run_id,
            mime_type="application/yaml", generated_metadata=_gen_meta(),
        )
