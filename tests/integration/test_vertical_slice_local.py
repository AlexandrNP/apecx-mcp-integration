"""T01 Phase 1 — vertical-slice integration test (steps 1-4).

The full AP §5.1 eight-step flow is:

    1. MCP tool start_workflow(description)
    2. Tier 3 composer generates YAML
    3. Tier 2 persists the run + diff, awaits approval
    4. User approves
    5. Tier 4 local executor runs first step
    6. Partial result → HITL gate
    7. Final steps run → output artifact
    8. Provenance log validates

This test covers **steps 1-4** end-to-end via the HTTP surface with a
placeholder LLM (no Ollama dependency). Steps 5-8 require a Tier 4
local-executor wiring that does not yet exist in this repo — see
implementation_plan.md §T01 P2.

What this test proves:
    - /workflows/start accepts a description and wires into the
      composer.
    - Composer writes Artifact + GeneratedArtifact rows via its
      ArtifactStore, linked to the Run the route created.
    - T06 approval-policy outcome is reflected in run.status
      (PAUSED on any novel Python; RUNNING when everything composes).
    - /workflows/diff returns the categorization the composer
      persisted.

No mocks beyond the placeholder LLM (per the workspace mocks-policy:
the LLM backend is an external dependency, and the live-LLM case is
covered by ``test_composer_phase2_against_ollama.py``).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apecx_integration.composition.approval_policy import ApprovalPolicy
from apecx_integration.composition.artifact_store import ArtifactStore
from apecx_integration.composition.composer import Composer
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import (
    ProvenanceRecorder,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
)
DEFAULT_POLICY = REPO_ROOT / "configs" / "approval_policy.yml"


# ---------------------------------------------------------------------------
# Placeholder LLM — returns a canned fenced YAML block
# ---------------------------------------------------------------------------


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


COMPOSED_ONLY_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: vertical_slice_composed
    description: "Composed workflow — only library components."
    version: "0.1.0"
    steps:
      extract:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
    links: {}
    ```
    """
)

MIXED_WITH_NOVEL_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: vertical_slice_mixed
    description: "Mixed composed + novel workflow."
    version: "0.1.0"
    steps:
      extract:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
      custom_postproc:
        class: "generated.CustomPostproc"
        config: {}
    links: {}
    ```

    ```novel_python
    custom_postproc: |
      class CustomPostproc:
          async def process(self, input_data, **kwargs):
              return {"formatted": str(input_data)}
    ```
    """
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_composer_with_store(engine: Engine, *, canned_response: str) -> Composer:
    factory = make_session_factory(engine)
    store = ArtifactStore(
        session_factory=factory,
        recorder=ProvenanceRecorder(factory),
    )
    composer = Composer.from_config(COMPOSER_CONFIG)
    composer._llm_factory = _make_factory(canned_response)
    composer._artifact_store = store
    return composer


@pytest.fixture
def policy() -> ApprovalPolicy:
    return ApprovalPolicy.load(DEFAULT_POLICY)


@pytest.fixture
def client_composed_only(cp_engine, policy) -> TestClient:
    """App with a composer that emits library-only workflows."""
    composer = _build_composer_with_store(
        cp_engine, canned_response=COMPOSED_ONLY_RESPONSE
    )
    app = create_app(
        engine=cp_engine, composer=composer, approval_policy=policy
    )
    return TestClient(app)


@pytest.fixture
def client_mixed_novel(cp_engine, policy) -> TestClient:
    """App with a composer that emits novel_python + library mix."""
    composer = _build_composer_with_store(
        cp_engine, canned_response=MIXED_WITH_NOVEL_RESPONSE
    )
    app = create_app(
        engine=cp_engine, composer=composer, approval_policy=policy
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Steps 1-3: start, compose, persist
# ---------------------------------------------------------------------------


def _start_body(description: str) -> dict:
    return {
        "description": description,
        "user_id": "alex",
        "preferred_executor": "local",
    }


def test_start_workflow_all_composed_auto_approves_and_runs(
    client_composed_only: TestClient,
):
    """Library-only workflow → policy AUTO → run.status = RUNNING."""
    response = client_composed_only.post(
        "/workflows/start",
        json=_start_body("extract viral entities from a query"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "running"
    assert body["generated_workflow_artifact_id"]
    assert body["run"]["started_at"] is not None


def test_start_workflow_with_novel_python_pauses_for_review(
    client_mixed_novel: TestClient,
):
    """Novel Python present → policy requires expert review →
    run.status = PAUSED (the reviewer must act before execution)."""
    response = client_mixed_novel.post(
        "/workflows/start",
        json=_start_body("extract and post-process entities"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "paused"
    assert body["run"]["started_at"] is None


def test_start_workflow_persists_artifact_reachable_via_diff(
    client_mixed_novel: TestClient,
):
    """Step 3 gate: the generated artifact is retrievable via the
    T06 ``/workflows/diff`` endpoint — same Run + Artifact row."""
    start = client_mixed_novel.post(
        "/workflows/start",
        json=_start_body("extract pathogens with postproc"),
    )
    assert start.status_code == 200, start.text
    run_id = start.json()["run"]["id"]
    artifact_id = start.json()["generated_workflow_artifact_id"]

    diff = client_mixed_novel.post(
        "/workflows/diff", json={"run_id": run_id}
    )
    assert diff.status_code == 200, diff.text
    body = diff.json()

    assert body["yaml_text"].startswith("name: vertical_slice_mixed")
    assert "custom_postproc" in body["novel_python_by_step"]
    # T06 categorization surfaces both steps.
    by_id = {c["step_id"]: c["category"] for c in body["categorization"]}
    # "extract" matches the library class path — should be composed_*.
    assert by_id["extract"].startswith("composed")
    # "custom_postproc" is in novel_python → novel.
    assert by_id["custom_postproc"] == "novel"
    assert "1 step(s) are novel Python" in body["summary_sentence"]

    # Artifact round-trips as a real UUID.
    UUID(artifact_id)


# ---------------------------------------------------------------------------
# Configuration error paths
# ---------------------------------------------------------------------------


def test_start_workflow_without_composer_returns_503(cp_engine):
    """Deployments that don't wire a composer shouldn't 500-crash —
    they should return a clear 503 explaining the missing config."""
    app = create_app(engine=cp_engine)  # no composer, no policy
    client = TestClient(app)
    response = client.post(
        "/workflows/start",
        json=_start_body("anything"),
    )
    assert response.status_code == 503
    assert "Composer is not configured" in response.json()["detail"]


def test_start_workflow_without_policy_returns_503(cp_engine):
    """Composer present but no approval policy → 503 on the policy
    dependency, not a 500 from a None-attr access."""
    composer = _build_composer_with_store(
        cp_engine, canned_response=COMPOSED_ONLY_RESPONSE
    )
    app = create_app(engine=cp_engine, composer=composer)
    client = TestClient(app)
    response = client.post(
        "/workflows/start",
        json=_start_body("anything"),
    )
    assert response.status_code == 503
    assert "ApprovalPolicy is not configured" in response.json()["detail"]
