"""T-COMP Phase 3 tests — ArtifactStore persistence integration.

Exercises the with-run-id persist path against a real migrated
SQLite DB + real ArtifactStore (no mocks — per workspace policy
§"Mocks Carve-Out"). Fixture shape mirrors
``test_artifact_store.py``: migrated DB + one Run row so the FK
constraint holds when Artifact is inserted.

Scope asserted here (spec §5 AC3):
- Artifact row exists for the composed YAML.
- On-disk file exists at ``artifact.location`` and its sha256
  matches the row's ``content_hash``.
- GeneratedArtifact row pins the prompt / library_version /
  llm_model / llm_model_version_hash / composition_summary.
- The returned ``ComposedWorkflow.artifact_id`` equals the Artifact
  row's UUID.
- A ``WORKFLOW_GENERATED`` provenance event is emitted under the
  run's hash chain (auto-hooked inside ArtifactStore.store()).

Spec §5 AC4 partial coverage:
- Two successive compose() calls produce distinct artifact IDs.
- Same-content emissions produce the same content_hash but different
  Artifact UUIDs (append-only invariant).

Not covered here (future phases):
- Phase 4 RAG swap-in.
- Phase 5 composition-bias regression measurement.
- Live Ollama persist path (operator-run; see Phase 3 follow-up if
  we want one).
"""

from __future__ import annotations

import asyncio
import hashlib
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select, text

from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.composition.composer import Composer
from apecx_integration.control_plane.db import (
    make_engine,
    make_session_factory,
)
from apecx_integration.control_plane.models.entities import (
    Artifact as ArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    GeneratedArtifact as GeneratedArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    ProvenanceEvent as ProvenanceEventORM,
)
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)


# ---------------------------------------------------------------------------
# DB + store fixtures (mirrors test_artifact_store.py)
# ---------------------------------------------------------------------------

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
def live_store(tmp_path):
    """Real ArtifactStore backed by real migrated SQLite + a seeded Run."""
    engine, run_id = _seeded_engine_and_run(tmp_path)
    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)
    artifact_root = tmp_path / "artifact_root"
    store = ArtifactStore(session_factory, recorder, root=artifact_root)
    return store, engine, run_id, session_factory


# ---------------------------------------------------------------------------
# Placeholder LLM (same shape as phase-2 tests — lets us drive compose()
# end-to-end without a network call)
# ---------------------------------------------------------------------------

HAPPY_PATH_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: p3_test_workflow
    description: "Test workflow for Phase-3 persistence integration."
    version: "0.1.0"
    steps:
      entity_extraction:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
    links: {}
    ```
    """
)


class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    def __init__(self, canned: str):
        self.canned = canned

    def invoke(self, messages):
        return _PlaceholderResponse(self.canned)


def _make_factory(canned: str):
    def _factory(**_kwargs):
        return _PlaceholderLLM(canned)
    return _factory


# ---------------------------------------------------------------------------
# AC3 — with run_id: persistence happens, rows + file + provenance line up
# ---------------------------------------------------------------------------

def test_compose_with_run_id_persists_artifact_and_generated_metadata(live_store):
    store, engine, run_id, session_factory = live_store
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = _make_factory(HAPPY_PATH_RESPONSE)
    composer._artifact_store = store

    prompt = "find entities then do something useful"
    result = asyncio.run(composer.compose(prompt, context={"run_id": run_id}))

    # AC3.1 — Artifact row exists + matches the returned artifact_id.
    with session_factory() as session:
        artifact = session.get(ArtifactORM, result.artifact_id)
        assert artifact is not None, (
            "expected Artifact row to exist after compose+persist"
        )
        assert artifact.run_id == run_id
        # content_hash on the row equals sha256(yaml_bytes)
        expected_hash = hashlib.sha256(result.yaml_bytes).hexdigest()
        assert artifact.content_hash == expected_hash

        # AC3.2 — on-disk file exists + its bytes rehash to the same hash.
        on_disk = Path(artifact.location)
        assert on_disk.is_file(), f"expected on-disk file at {on_disk}"
        assert hashlib.sha256(on_disk.read_bytes()).hexdigest() == expected_hash

        # AC3.3 — GeneratedArtifact row pins the metadata.
        gen = session.get(GeneratedArtifactORM, artifact.id)
        assert gen is not None, "expected GeneratedArtifact row for GENERATED_WORKFLOW"
        assert gen.source_prompt == prompt
        assert gen.library_version == composer.config.library_version
        assert gen.llm_model == composer.config.llm_model
        assert len(gen.llm_model_version_hash) == 64
        assert gen.composition_summary["steps_reused"] >= 0

        # AC3.4 — WORKFLOW_GENERATED provenance event is emitted.
        stmt = (
            select(ProvenanceEventORM)
            .where(ProvenanceEventORM.run_id == run_id)
            .where(
                ProvenanceEventORM.event_type == ProvenanceEventType.WORKFLOW_GENERATED
            )
        )
        events = session.execute(stmt).scalars().all()
        assert len(events) == 1
        payload = events[0].payload
        assert payload["artifact_id"] == str(artifact.id)
        assert payload["content_hash"] == expected_hash


def test_compose_with_run_id_yaml_parses_as_workflow(live_store):
    """Phase-3 persist path must still produce parseable YAML (the
    persist step doesn't mutate the content between parse-check and
    disk write)."""
    store, engine, run_id, session_factory = live_store
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = _make_factory(HAPPY_PATH_RESPONSE)
    composer._artifact_store = store

    result = asyncio.run(composer.compose("x", context={"run_id": run_id}))

    workflow = yaml.safe_load(result.yaml_bytes.decode("utf-8"))
    assert workflow["name"] == "p3_test_workflow"


# ---------------------------------------------------------------------------
# AC4 — regeneration produces distinct artifact IDs even with the
# same LLM output (append-only)
# ---------------------------------------------------------------------------

def test_two_composes_produce_distinct_artifact_ids(live_store):
    store, engine, run_id, session_factory = live_store
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = _make_factory(HAPPY_PATH_RESPONSE)
    composer._artifact_store = store

    result_a = asyncio.run(composer.compose("first", context={"run_id": run_id}))
    result_b = asyncio.run(composer.compose("second", context={"run_id": run_id}))

    assert result_a.artifact_id != result_b.artifact_id, (
        "AC4 requires distinct artifact IDs for successive compose() calls "
        "(append-only invariant — regenerate creates, never replaces)."
    )

    # And both rows exist.
    with session_factory() as session:
        assert session.get(ArtifactORM, result_a.artifact_id) is not None
        assert session.get(ArtifactORM, result_b.artifact_id) is not None


def test_same_content_same_hash_distinct_uuids(live_store):
    """If the LLM is deterministic (temperature=0) and emits the same
    yaml twice, the content_hash is equal but the row UUIDs are not.
    """
    store, engine, run_id, session_factory = live_store
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = _make_factory(HAPPY_PATH_RESPONSE)
    composer._artifact_store = store

    result_a = asyncio.run(composer.compose("same", context={"run_id": run_id}))
    result_b = asyncio.run(composer.compose("same", context={"run_id": run_id}))

    # Same LLM canned response → same content → same hash.
    assert result_a.yaml_bytes == result_b.yaml_bytes

    with session_factory() as session:
        a = session.get(ArtifactORM, result_a.artifact_id)
        b = session.get(ArtifactORM, result_b.artifact_id)
        assert a.content_hash == b.content_hash
        assert a.id != b.id


# ---------------------------------------------------------------------------
# Phase-2 compat: without run_id, legacy behavior (no persistence)
# ---------------------------------------------------------------------------

def test_compose_without_run_id_synthesizes_uuid_and_skips_persist(live_store):
    """When the store is injected but the caller omits run_id, the
    composer falls back to Phase-2 behavior (uuid4 + no DB write).

    Behavioral assertion: no Artifact row is created. A one-shot
    warning log fires (verified manually 2026-04-23; caplog capture
    doesn't work reliably through pytest-asyncio auto-mode + the
    three-attempt cap says stop debugging test-framework interactions).
    """
    store, engine, run_id, session_factory = live_store
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = _make_factory(HAPPY_PATH_RESPONSE)
    composer._artifact_store = store

    result = asyncio.run(composer.compose("no run_id here"))

    # No Artifact row should have been created (legacy behavior).
    with session_factory() as session:
        assert session.get(ArtifactORM, result.artifact_id) is None

    # The returned artifact_id is still a valid UUID (uuid4 fallback).
    assert result.artifact_id is not None


def test_compose_without_store_preserves_phase2_behavior():
    """If no ArtifactStore is injected, compose() works (no DB writes
    happen because there's no store to write to)."""
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = _make_factory(HAPPY_PATH_RESPONSE)
    # Don't set _artifact_store — it stays None.

    result = asyncio.run(composer.compose("whatever", context={"run_id": uuid4()}))
    assert result.artifact_id is not None
    assert len(result.yaml_bytes) > 0
