"""T01 P2 — LocalExecutor integration test.

End-to-end flow:

    POST /workflows/start (placeholder LLM) → Run(RUNNING) + Artifact
                   ↓
    LocalExecutor.execute(run_id)
                   ↓
    Run(COMPLETED) + OUTPUT Artifact + RUN_STARTED + RUN_COMPLETED
    provenance events in a validating hash chain

OR (when execution realistically fails because Ollama is unreachable):

    Run(FAILED) + RUN_STARTED + RUN_FAILED provenance events
    + readable reason in the RUN_FAILED payload.

Both paths are release-useful — the executor must capture whatever
happens and surface it cleanly.

Why this test requires the venv
-------------------------------
This suite imports ``nanobrain.core.workflow.Workflow`` and the
``apecx_db_integration`` package the composed workflows reference.
Both are installed editable into
``apecx-mcp-integration/.venv`` (see friction log #14 for the
"system Python vs. venv Python" signal). Running under the system
conda Python silently gives ``ModuleNotFoundError``.

Marker: ``integration``; auto-skip when nanobrain is not importable
so a cold CI env (no venv) doesn't red-build on a missing dep.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

try:
    import nanobrain.core.workflow  # noqa: F401

    _NANOBRAIN_AVAILABLE = True
except ImportError:
    _NANOBRAIN_AVAILABLE = False

from apecx_integration.composition.approval_policy import ApprovalPolicy
from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.composition.composer import Composer
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.executors.local import (
    LocalExecutor,
    run_sync,
)
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)
from apecx_integration.control_plane.schemas.enums import (
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
COMPOSER_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
# violin_bvbrc retired 2026-06-15; this test uses a surviving workflow
# dir purely as a generic base for the executor's relative step-config
# resolution. rag_e2e_synthesis ships a real ``steps/rag_synthesis.yml``.
EXAMPLE_WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "rag_e2e_synthesis"
)
DEFAULT_POLICY = REPO_ROOT / "configs" / "approval_policy.yml"


class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    def __init__(self, canned: str):
        self.canned = canned

    def invoke(self, messages):
        return _PlaceholderResponse(self.canned)


def _placeholder_factory(canned: str):
    def _factory(**_kwargs):
        return _PlaceholderLLM(canned)

    return _factory


# A workflow with a single step that references a real shipped wrapper
# YAML in the rag_e2e_synthesis manifest tree. Enough for nanobrain to
# attempt a load. Execution will fail at process() time (empty retrieval
# gate / Ollama unreachable in CI) — the executor catches that cleanly.
# Monolithic (yaml) composer format — see _build_composer, which pins
# the composer to monolithic mode to match this response shape.
ONE_STEP_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: local_executor_single_step_test
    description: "Single-step workflow for T01 P2 end-to-end test."
    version: "0.1.0"
    steps:
      synthesize:
        class: "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
        config: "steps/rag_synthesis.yml"
    links: {}
    ```
    """
)


def _build_composer(engine: Engine) -> Composer:
    factory = make_session_factory(engine)
    store = ArtifactStore(
        session_factory=factory,
        recorder=ProvenanceRecorder(factory),
    )
    composer = Composer.from_config(COMPOSER_CONFIG)
    # This test exercises the executor round-trip via a monolithic (yaml)
    # composer response; pin the mode so the default spec-mode parser
    # (which expects a JSON MinimalWorkflowSpec) doesn't reject it.
    composer._config.composer_mode = "monolithic"
    composer._llm_factory = _placeholder_factory(ONE_STEP_RESPONSE)
    composer._artifact_store = store
    return composer


@pytest.fixture
def executor(cp_engine) -> LocalExecutor:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(session_factory=factory, recorder=recorder)
    return LocalExecutor(
        session_factory=factory,
        artifact_store=store,
        recorder=recorder,
        workflow_base_dir=EXAMPLE_WORKFLOW_DIR,
    )


@pytest.fixture
def client_with_composer(cp_engine) -> TestClient:
    composer = _build_composer(cp_engine)
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    app = create_app(engine=cp_engine, composer=composer, approval_policy=policy)
    return TestClient(app)


def _start_and_get_run_id(client: TestClient) -> str:
    response = client.post(
        "/workflows/start",
        json={
            "description": "extract pathogen entities",
            "user_id": "alex",
            "preferred_executor": "local",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["run"]["id"]


def _events_for_run(cp_engine: Engine, run_id: str) -> list[str]:
    with cp_engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT event_type FROM provenance_event WHERE run_id = :rid ORDER BY timestamp"
                ),
                {"rid": run_id},
            )
        )
    return [r[0] for r in rows]


def _run_status(cp_engine: Engine, run_id: str) -> str:
    with cp_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM run WHERE id = :rid"),
            {"rid": run_id},
        ).first()
    assert row is not None
    return row[0].lower()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_executor_captures_run_started_event_before_loading(
    client_with_composer: TestClient,
    executor: LocalExecutor,
    cp_engine: Engine,
):
    """RUN_STARTED must be emitted before load attempts so partial
    failures have a chain anchor. Load may succeed or fail; either
    way RUN_STARTED is present."""
    run_id = _start_and_get_run_id(client_with_composer)
    run_sync(executor, run_id)
    events = [e.lower() for e in _events_for_run(cp_engine, run_id)]
    assert "run_started" in events, f"expected RUN_STARTED in events; got {events}"
    # RUN_STARTED must come BEFORE any terminal (COMPLETED/FAILED) event
    # so the chain anchor is present if execution fails partway.
    idx_started = events.index("run_started")
    for terminal in ("run_completed", "run_failed"):
        if terminal in events:
            assert events.index(terminal) > idx_started, (
                f"RUN_STARTED must precede {terminal}; got {events}"
            )


def test_executor_captures_terminal_event_and_updates_run_status(
    client_with_composer: TestClient,
    executor: LocalExecutor,
    cp_engine: Engine,
):
    """Every execution ends with either RUN_COMPLETED or RUN_FAILED,
    and the run.status must match. Whichever branch fires, the chain
    validates."""
    run_id = _start_and_get_run_id(client_with_composer)
    result = run_sync(executor, run_id)
    events = _events_for_run(cp_engine, run_id)

    terminal_ok = {"run_completed", "run_failed"}
    lowered = [e.lower() for e in events]
    assert lowered[-1] in terminal_ok, f"expected terminal event; got {events}"

    status = _run_status(cp_engine, run_id)
    if lowered[-1] == "run_completed":
        assert status == "completed"
        assert result.status is RunStatus.COMPLETED
        assert result.output_artifact_id is not None
    else:
        assert status == "failed"
        assert result.status is RunStatus.FAILED
        assert result.reason  # non-empty

    # Chain must validate regardless of success/failure branch.
    executor._recorder.validate(result.run_id)


def test_executor_marks_failed_when_run_has_no_workflow_config(
    cp_engine: Engine, executor: LocalExecutor
):
    """Preconditions failure is its own class — no Artifact to load.
    Must still end with RUN_FAILED + readable reason."""
    import uuid

    run_id = uuid.uuid4()
    from datetime import UTC, datetime

    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )

    result = run_sync(executor, run_id)
    assert result.status is RunStatus.FAILED
    assert "workflow_misconfigured" in (result.reason or "")
    assert _run_status(cp_engine, str(run_id)) == "failed"


def test_executor_provenance_failure_payload_records_class(
    cp_engine: Engine, executor: LocalExecutor
):
    """RUN_FAILED must carry a structured ``failure_class`` so
    downstream UIs can bucket error types without regex-matching
    the reason string.

    Pre-2026-04-27 this test ran the full composer + run path and
    skipped if the run happened to succeed (relying on Ollama being
    unreachable). On a dev machine with Ollama running, the test
    silently always-skipped — never asserting on the failure_class
    contract. That is the silent-failure shape the user-directive
    guards against.

    Rewritten to FORCE a deterministic failure using the same
    "no workflow_config artifact" precondition as
    ``test_executor_marks_failed_when_run_has_no_workflow_config``.
    Now the test runs (and asserts) on every CI / dev run.
    """
    import json
    import uuid
    from datetime import UTC, datetime

    run_id = uuid.uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )

    result = run_sync(executor, run_id)
    assert result.status is RunStatus.FAILED, (
        f"forced precondition failure must land in FAILED; got {result.status}"
    )

    with cp_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT payload FROM provenance_event "
                "WHERE run_id = :rid AND event_type = 'RUN_FAILED'"
            ),
            {"rid": str(run_id)},
        ).first()
    assert row is not None, "RUN_FAILED event missing from provenance"
    payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert "failure_class" in payload, f"failure_class missing from RUN_FAILED payload: {payload!r}"
    assert payload["failure_class"] in {
        "load_failed",
        "execute_failed",
        "workflow_misconfigured",
    }, f"unexpected failure_class: {payload['failure_class']!r}"
    assert "reason" in payload
    assert payload["reason"]  # non-empty
