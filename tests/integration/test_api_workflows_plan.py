"""T01 supporting closeout — ``/workflows/plan`` preview-mode composition.

Mirrors the ``test_vertical_slice_local.py`` composer-injection pattern
but targets the preview endpoint instead of start. Asserts:
- The endpoint returns a valid GeneratePlanResponse.
- The Run row the composer wrote is CANCELLED (not RUNNING / PAUSED).
- The plan list echoes the T06 categorization.
- The yaml_text in the response matches what the composer produced.
- 503 paths for missing composer dependency (no policy needed —
  preview doesn't evaluate approval).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

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


MIXED_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: plan_test_mixed
    description: "mixed composed + novel"
    version: "0.1.0"
    steps:
      extract:
        class: "apecx_db_integration.agent.extract_entities_llm"
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
              return {}
    ```
    """
)


def _build_composer(engine: Engine, *, canned: str) -> Composer:
    factory = make_session_factory(engine)
    store = ArtifactStore(
        session_factory=factory,
        recorder=ProvenanceRecorder(factory),
    )
    composer = Composer.from_config(COMPOSER_CONFIG)
    composer._llm_factory = _placeholder_factory(canned)
    composer._artifact_store = store
    return composer


@pytest.fixture
def plan_client(cp_engine) -> TestClient:
    composer = _build_composer(cp_engine, canned=MIXED_RESPONSE)
    # No approval_policy needed for /plan — preview doesn't gate.
    app = create_app(engine=cp_engine, composer=composer)
    return TestClient(app)


def test_plan_returns_yaml_and_categorization(
    plan_client: TestClient, cp_engine: Engine
):
    response = plan_client.post(
        "/workflows/plan",
        json={"description": "extract entities with a postproc step"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["yaml_text"].startswith("name: plan_test_mixed")
    assert body["generated_artifact_id"]
    categories = {p["step_id"]: p["category"] for p in body["plan"]}
    assert categories["extract"].startswith("composed")
    assert categories["custom_postproc"] == "novel"


def test_plan_marks_backing_run_cancelled(
    plan_client: TestClient, cp_engine: Engine
):
    """Brutal-truth invariant: /plan creates a Run row (because the
    ArtifactStore requires a Run FK), but that row must be CANCELLED
    — no live execution picks up a preview."""
    response = plan_client.post(
        "/workflows/plan",
        json={"description": "preview this workflow"},
    )
    assert response.status_code == 200, response.text

    with cp_engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT status, user_id, completed_at FROM run "
                    "WHERE user_id = '_preview'"
                )
            )
        )
    assert len(rows) == 1
    status_val, user_id, completed_at = rows[0]
    assert status_val.lower() == "cancelled"
    assert user_id == "_preview"
    assert completed_at is not None


def test_plan_without_composer_returns_503(cp_engine: Engine):
    app = create_app(engine=cp_engine)  # no composer
    client = TestClient(app)
    response = client.post(
        "/workflows/plan",
        json={"description": "does not matter"},
    )
    assert response.status_code == 503
    assert "Composer is not configured" in response.json()["detail"]


def test_plan_works_without_approval_policy(cp_engine: Engine):
    """Preview mode doesn't need an approval policy. Wiring requires
    composer only; missing policy must NOT 503 here (as it does for
    /workflows/start)."""
    composer = _build_composer(cp_engine, canned=MIXED_RESPONSE)
    app = create_app(engine=cp_engine, composer=composer)
    client = TestClient(app)
    response = client.post(
        "/workflows/plan",
        json={"description": "preview"},
    )
    assert response.status_code == 200, response.text
