"""Integration tests for /workflows/start structured error observability.

Branch cp-error-observability: a KNOWN composition failure
(ComposerResponseError / ComposerConfigurationError / ScanViolation)
becomes a STRUCTURED 422 carrying the cause, while any other exception
keeps the bare re-raise (500). Both paths must still mark the persisted
Run row FAILED (no PENDING orphan).

Real migrated SQLite DB via the ``cp_engine`` fixture (no mocks). The
composer is a small in-process stub because we are exercising the
route's exception-handling branch, not the LLM — the composer's real
failure modes (ComposerResponseError, etc.) are the injected inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apecx_integration.composition._errors import ComposerResponseError
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.executors.local import ExecutionResult
from apecx_integration.control_plane.models.entities import Run as RunORM
from apecx_integration.control_plane.schemas.enums import RunStatus

pytestmark = pytest.mark.integration


class _RaisingComposer:
    """Composer stub whose compose() raises a KNOWN composition failure."""

    async def compose(self, description, context=None):
        raise ComposerResponseError(
            "spec mode: expander could not realize the spec: step 'x': "
            "class_name 'RagDomainSearchOnly' has no catalog match."
        )


class _BoomComposer:
    """Composer stub whose compose() raises an UNEXPECTED error."""

    async def compose(self, description, context=None):
        raise RuntimeError("boom")


class _StubPolicy:
    """Minimal approval policy — ``require_approval_policy`` only checks
    non-None, and compose() raises before ``policy.evaluate`` is reached.
    """


def _only_run_row(cp_engine) -> RunORM:
    session_factory = make_session_factory(cp_engine)
    with session_factory() as session:
        rows = session.query(RunORM).all()
        assert len(rows) == 1, f"expected exactly one run row, got {len(rows)}"
        return rows[0]


def test_start_workflow_composition_failure_returns_structured_422(cp_engine) -> None:
    app = create_app(
        engine=cp_engine,
        composer=_RaisingComposer(),
        approval_policy=_StubPolicy(),
    )
    client = TestClient(app)

    resp = client.post("/workflows/start", json={"description": "x", "user_id": "alex"})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "workflow composition failed" in detail, detail
    assert "has no catalog match" in detail, detail

    run = _only_run_row(cp_engine)
    assert run.status is RunStatus.FAILED
    assert run.completed_at is not None


def test_start_workflow_unexpected_error_still_500(cp_engine) -> None:
    app = create_app(
        engine=cp_engine,
        composer=_BoomComposer(),
        approval_policy=_StubPolicy(),
    )
    # raise_server_exceptions=False so the bare re-raise surfaces as a 500
    # response instead of propagating into the test.
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/workflows/start", json={"description": "x", "user_id": "alex"})

    assert resp.status_code == 500, resp.text

    run = _only_run_row(cp_engine)
    assert run.status is RunStatus.FAILED
    assert run.completed_at is not None


# --- /workflows/execute NOT-FOUND → 404 (branch execute-404-nonexistent-run) ---
#
# execute_workflow FIRST calls require_local_executor(executor) (503 if None),
# THEN reads the DB and raises 404 when the run does not exist. A run that
# EXISTS but fails execution still returns 200 + status="failed" (unchanged).
# So we MUST inject a non-None executor; the 404 test never reaches its
# execute() (404 raised first), the not-404 test does reach it.


class _StubExecutor:
    """LocalExecutor stand-in — the injected dependency (not a bypassed real
    dep). ``require_local_executor`` only checks non-None; the route's 404
    precondition + DB read are exercised for real against ``cp_engine``.
    """

    def __init__(self, result: ExecutionResult) -> None:
        self._result = result

    async def execute(self, run_id) -> ExecutionResult:
        return self._result


def _insert_run(cp_engine, run_id, *, status: str = "RUNNING") -> None:
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) VALUES (:id, :uid, :status, :ts)"
            ),
            {
                "id": str(run_id),
                "uid": "alex",
                "status": status,
                "ts": datetime.now(UTC).isoformat(),
            },
        )


def test_execute_nonexistent_run_returns_404(cp_engine) -> None:
    """No run inserted → the 404 precondition fires and the executor is never
    reached (the stub result would be COMPLETED, so a 404 proves not-found
    short-circuits before execute()).
    """
    unreachable = ExecutionResult(
        run_id=uuid4(),
        status=RunStatus.COMPLETED,
        reason=None,
        output_artifact_id=None,
    )
    app = create_app(engine=cp_engine, local_executor=_StubExecutor(unreachable))
    client = TestClient(app, raise_server_exceptions=False)

    missing_id = uuid4()
    resp = client.post("/workflows/execute", json={"run_id": str(missing_id)})

    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"], resp.json()["detail"]


def test_execute_existing_run_is_not_404(cp_engine) -> None:
    """A run that EXISTS reaches the executor: 200 (NOT 404), proving the
    precondition only fires for genuine not-found.
    """
    run_id = uuid4()
    _insert_run(cp_engine, run_id, status="RUNNING")

    result = ExecutionResult(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        reason=None,
        output_artifact_id=None,
    )
    app = create_app(engine=cp_engine, local_executor=_StubExecutor(result))
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/workflows/execute", json={"run_id": str(run_id)})

    assert resp.status_code == 200, resp.text
    assert resp.status_code != 404
    assert resp.json()["status"] == RunStatus.COMPLETED.value


def test_execute_existing_run_that_fails_is_still_200_not_404(cp_engine) -> None:
    """A run that EXISTS but whose execution FAILS still returns 200 + status='failed'
    — the 404 is existence-only, NOT a proxy for failure. Makes the differential airtight
    (contrast test_execute_nonexistent_run_returns_404)."""
    run_id = uuid4()
    _insert_run(cp_engine, run_id, status="RUNNING")

    result = ExecutionResult(
        run_id=run_id,
        status=RunStatus.FAILED,
        reason="workflow step raised",
        output_artifact_id=None,
    )
    app = create_app(engine=cp_engine, local_executor=_StubExecutor(result))
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/workflows/execute", json={"run_id": str(run_id)})

    assert resp.status_code == 200, resp.text  # a real FAILED is 200, not 404
    body = resp.json()
    assert body["status"] == RunStatus.FAILED.value
    assert body["reason"] == "workflow step raised"
