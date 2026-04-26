"""Cluster AI — /workflows/start orphans run if commit-3 fails.

Cluster U fixed one orphan-PENDING bug: if ``composer.compose()``
raises, the run stays PENDING forever (sweeper only sweeps
RUNNING/PAUSED). The fix wrapped ``compose()`` with a try/except
that flips the run to FAILED before re-raising.

But the route's commit-3 block — the back-link to the artifact
and the status transition — is still unguarded. If anything
between the successful compose() and the final commit raises,
the run is orphaned in PENDING:

    try:
        composed = await composer.compose(...)   # cluster U fix
    except Exception:
        ...mark FAILED, re-raise...

    with session_factory() as session:
        run = session.get(...)
        run.workflow_config_id = composed.artifact_id

        decision = policy.evaluate(...)              # may raise
        run.status = RUNNING / PAUSED                 # ORM dirty
        session.commit()                              # may raise (disk full)

The same gap exists in ``/workflows/plan``.

The realistic triggers for commit-3 raising:
  - ``policy.evaluate`` raises on a malformed composition_summary
    (LLM produced a step the policy doesn't understand);
  - ``session.commit`` raises on disk-full / WAL-corruption /
    Postgres connection drop / SQLITE_BUSY (the FastAPI threadpool
    can hold connections under load);
  - any new validation logic added between commit 1 and commit 3
    that surfaces as an exception.

Fix: extend the try/except to cover EVERYTHING between commit 1
(insert PENDING) and commit 3 (terminal transition out of
PENDING). On any exception, mark the run FAILED via a fresh
session, then re-raise.

This test simulates the failure mode by monkey-patching
``policy.evaluate`` to raise. After the route raises, the run
must be FAILED (not stuck in PENDING).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def test_workflows_start_marks_failed_when_post_compose_raises(
    cp_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Patch ``policy.evaluate`` to raise. Confirm the run row
    transitions PENDING → FAILED instead of leaking in PENDING.
    """
    from apecx_integration.composition.approval_policy import ApprovalPolicy
    from apecx_integration.composition.composer_schemas import (
        ComposedWorkflow,
        CompositionSummary,
    )
    from apecx_integration.composition.differ import (
        StepCategorization,
        StepCategory,
    )
    from apecx_integration.control_plane.app import create_app

    # A composer that produces a fake successful result without
    # actually running an LLM. The route calls ``compose()`` and
    # passes the result to ``policy.evaluate``; we'll make the
    # POLICY raise.
    class _FakeComposer:
        async def compose(self, description, *, context=None):
            return ComposedWorkflow(
                artifact_id=UUID("11111111-1111-4111-8111-111111111111"),
                yaml_bytes=b"steps: {}\n",
                novel_python={},
                composition_summary=CompositionSummary(
                    steps_reused=0,
                    steps_generated=1,
                    steps_swapped=0,
                    summary_sentence="fake composition",
                    step_categorizations=(
                        StepCategorization(
                            step_id="s1",
                            step_class="apecx_integration.fake.Step",
                            category=StepCategory.NOVEL,
                            reason="fake",
                        ),
                    ),
                ),
                retrieved_components=(),
                llm_model="fake",
                llm_model_version_hash="0" * 64,
            )

    # We don't pre-insert the artifact row. The route sets
    # ``run.workflow_config_id`` BEFORE calling policy.evaluate,
    # so when policy.evaluate raises the in-memory mutation is
    # rolled back when the session exits — no FK is ever
    # validated. The exception propagates through the route's
    # outer try/except (cluster U fix) which we're extending.

    # Construct a minimal ApprovalPolicy directly (the loader's
    # YAML mapping path is irrelevant here — we're just providing
    # an object whose ``evaluate`` method we'll patch to raise).
    from apecx_integration.composition.approval_policy import ApprovalAction
    from apecx_integration.composition.differ import StepCategory as _StepCategory

    policy = ApprovalPolicy(
        mapping={cat: ApprovalAction.AUTO for cat in _StepCategory}
    )

    def _boom(_categorized):
        raise RuntimeError("simulated policy.evaluate failure")

    monkeypatch.setattr(policy, "evaluate", _boom)

    app = create_app(
        engine=cp_engine,
        composer=_FakeComposer(),
        approval_policy=policy,
    )
    client = TestClient(app, raise_server_exceptions=False)

    # Hit /workflows/start. The composer's fake compose returns
    # successfully; policy.evaluate then raises; the route should
    # mark the run FAILED and propagate a 500.
    resp = client.post(
        "/workflows/start",
        json={"user_id": "alex", "description": "test"},
    )
    assert resp.status_code in (500, 422), (
        f"expected 500 from policy.evaluate raising, got {resp.status_code}: {resp.text}"
    )

    # Inspect the run row. It MUST be FAILED, not PENDING.
    with cp_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, status FROM run WHERE user_id = 'alex'")
        ).fetchall()

    assert len(rows) == 1, f"expected 1 run row, got {len(rows)}: {rows}"
    run_id_str, status = rows[0]
    print(f"\n[start-orphan2] run_id={run_id_str} status={status}")
    assert status == "FAILED", (
        f"BUG: /workflows/start exception in commit-3 block left run "
        f"{run_id_str} in status={status} instead of FAILED. The "
        "cluster U fix wrapped only the compose() block; this "
        "commit-3 block is unguarded. Result: the run is orphaned "
        "in PENDING (sweeper only sweeps RUNNING/PAUSED, see "
        "cluster Z). Fix: extend the cluster U try/except to "
        "cover everything between commit 1 and commit 3."
    )
