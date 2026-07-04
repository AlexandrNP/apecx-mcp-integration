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

import pytest
from fastapi.testclient import TestClient

from apecx_integration.composition._errors import ComposerResponseError
from apecx_integration.control_plane.app import create_app
from apecx_integration.control_plane.db import make_session_factory
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
