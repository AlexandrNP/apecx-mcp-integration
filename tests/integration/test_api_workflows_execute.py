"""HTTP surface for the LocalExecutor (T01 P2 follow-up).

Completes the round-trip: ``/workflows/start`` creates a Run;
``/workflows/execute`` runs it and returns the terminal state.
Verifies both the happy-ish (load OK, execution fails at first LLM
call, RUN_FAILED persisted) and the no-executor-configured (503) paths.

Gated on nanobrain importability — mirrors
``test_local_executor.py``'s skip policy. Run under the venv:
``.venv/bin/python -m pytest tests/integration/test_api_workflows_execute.py``.
"""

from __future__ import annotations

import textwrap
import uuid
from datetime import UTC, datetime
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
from apecx_integration.control_plane.executors.local import LocalExecutor
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
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
# violin_bvbrc retired 2026-06-15; use a surviving workflow dir as the
# generic base for the executor's relative step-config resolution.
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


# Monolithic (yaml) composer format — see client_full, which pins the
# composer to monolithic mode to match this response shape.
ONE_STEP_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: execute_api_test_single_step
    description: "T01 P2 HTTP surface test."
    version: "0.1.0"
    steps:
      synthesize:
        class: "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep"
        config: "steps/rag_synthesis.yml"
    links: {}
    ```
    """
)


@pytest.fixture
def client_full(cp_engine) -> TestClient:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(session_factory=factory, recorder=recorder)
    composer = Composer.from_config(COMPOSER_CONFIG)
    # Pin monolithic mode so the yaml ONE_STEP_RESPONSE parses (the
    # default spec mode expects a JSON MinimalWorkflowSpec).
    composer._config.composer_mode = "monolithic"
    composer._llm_factory = _placeholder_factory(ONE_STEP_RESPONSE)
    composer._artifact_store = store
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    executor = LocalExecutor(
        session_factory=factory,
        artifact_store=store,
        recorder=recorder,
        workflow_base_dir=EXAMPLE_WORKFLOW_DIR,
    )
    app = create_app(
        engine=cp_engine,
        composer=composer,
        approval_policy=policy,
        local_executor=executor,
    )
    return TestClient(app)


def test_execute_round_trip_from_start_to_terminal(client_full: TestClient, cp_engine: Engine):
    """Full story: POST /workflows/start → POST /workflows/execute →
    response carries the terminal status and the Run row matches."""
    start = client_full.post(
        "/workflows/start",
        json={
            "description": "extract pathogen entities",
            "user_id": "alex",
            "preferred_executor": "local",
        },
    )
    assert start.status_code == 200, start.text
    run_id = start.json()["run"]["id"]

    execute = client_full.post("/workflows/execute", json={"run_id": run_id})
    assert execute.status_code == 200, execute.text
    body = execute.json()
    assert body["status"] in {"completed", "failed"}
    assert body["run_id"] == run_id

    # Response and DB must agree on terminal status.
    with cp_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM run WHERE id = :rid"),
            {"rid": run_id},
        ).first()
    assert row is not None
    assert row[0].lower() == body["status"]


def test_execute_on_missing_run_returns_failed_with_reason(
    client_full: TestClient, cp_engine: Engine
):
    """Phantom run_id → LocalExecutor treats as workflow_misconfigured
    (no artifact to load), returns FAILED with a reason. 200 from the
    HTTP surface (the executor ran to completion of a kind)."""
    phantom = uuid.uuid4()
    # Pre-insert a Run row with no workflow_config_id so the executor
    # has a row to mark FAILED (it only uses the provided run_id).
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(phantom), "ts": datetime.now(UTC).isoformat()},
        )

    execute = client_full.post("/workflows/execute", json={"run_id": str(phantom)})
    assert execute.status_code == 200, execute.text
    body = execute.json()
    assert body["status"] == "failed"
    assert body["reason"]


def test_execute_without_local_executor_returns_503(cp_engine: Engine):
    """Control Plane built without a local_executor → 503 with the
    standard 'not configured' detail string."""
    app = create_app(engine=cp_engine)
    client = TestClient(app)
    response = client.post(
        "/workflows/execute",
        json={"run_id": str(uuid.uuid4())},
    )
    assert response.status_code == 503
    assert "LocalExecutor is not configured" in response.json()["detail"]
