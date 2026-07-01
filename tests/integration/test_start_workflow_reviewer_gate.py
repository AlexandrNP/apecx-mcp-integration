"""#6 route wiring (2026-07-01) — the semantic reviewer's verdict gates the run.

A composition whose steps are ALL auto-approvable by category (``composed_standard`` → AUTO) still
lands ``PAUSED`` at ``/workflows/start`` when the reviewer REJECTED it, and ``RUNNING`` when the
reviewer approved. Before the fix the verdict was advisory and a rejected-but-all-auto workflow ran.
Reuses the fake-composer + ``cp_engine`` harness pattern from
``test_async_start_post_compose_orphan.py`` (no LLM).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fake_composer(engine: Engine, review_verdict: dict):
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from apecx_integration.composition.composer_schemas import (
        ComposedWorkflow,
        CompositionSummary,
    )
    from apecx_integration.composition.differ import StepCategorization, StepCategory
    from apecx_integration.control_plane.models.entities import Artifact as ArtifactORM
    from apecx_integration.control_plane.schemas.enums import ArtifactKind

    class _FakeComposer:
        async def compose(self, description, *, context=None):
            run_id = context["run_id"]
            artifact_id = uuid4()
            # Persist a minimal artifact so the route's run.workflow_config_id FK (→ artifact.id)
            # is satisfied on commit — the real composer does this via its ArtifactStore, using the
            # run_id passed in the compose() context.
            with Session(engine) as s:
                s.add(
                    ArtifactORM(
                        id=artifact_id,
                        run_id=run_id,
                        step_id=None,
                        kind=ArtifactKind.GENERATED_WORKFLOW,
                        location="mem://fake",
                        content_hash="0" * 64,
                        size_bytes=1,
                        mime_type="text/yaml",
                        created_at=datetime.now(UTC),
                    )
                )
                s.commit()
            return ComposedWorkflow(
                artifact_id=artifact_id,
                yaml_bytes=b"steps: {}\n",
                novel_python={},
                composition_summary=CompositionSummary(
                    steps_reused=1,
                    steps_generated=0,
                    steps_swapped=0,
                    summary_sentence="all auto",
                    step_categorizations=(
                        StepCategorization(
                            step_id="s1",
                            step_class="apecx_integration.fake.Step",
                            category=StepCategory.COMPOSED_STANDARD,  # → AUTO by default policy
                            reason="fake",
                        ),
                    ),
                    review_verdict=review_verdict,
                ),
                retrieved_components=(),
                llm_model="fake",
                llm_model_version_hash="0" * 64,
            )

    return _FakeComposer()


def _client(cp_engine: Engine, review_verdict: dict) -> TestClient:
    from apecx_integration.composition.approval_policy import ApprovalPolicy
    from apecx_integration.control_plane.app import create_app

    policy = ApprovalPolicy.load(REPO_ROOT / "configs" / "approval_policy.yml")
    app = create_app(
        engine=cp_engine,
        composer=_fake_composer(cp_engine, review_verdict),
        approval_policy=policy,
    )
    return TestClient(app, raise_server_exceptions=False)


def _run_status(cp_engine: Engine, user_id: str) -> str:
    with cp_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status FROM run WHERE user_id = :u"), {"u": user_id}
        ).fetchall()
    assert len(rows) == 1, f"expected 1 run row, got {rows}"
    return rows[0][0]


def test_start_workflow_pauses_on_reviewer_reject(cp_engine: Engine):
    client = _client(
        cp_engine,
        {"approved": False, "reasoning": "semantic mismatch", "concerns": [], "review_used": True},
    )
    resp = client.post("/workflows/start", json={"user_id": "reject_user", "description": "x"})
    assert resp.status_code < 300, resp.text
    assert _run_status(cp_engine, "reject_user") == "PAUSED"


def test_start_workflow_runs_on_reviewer_approve(cp_engine: Engine):
    client = _client(
        cp_engine,
        {"approved": True, "reasoning": "looks right", "concerns": [], "review_used": True},
    )
    resp = client.post("/workflows/start", json={"user_id": "approve_user", "description": "x"})
    assert resp.status_code < 300, resp.text
    assert _run_status(cp_engine, "approve_user") == "RUNNING"
