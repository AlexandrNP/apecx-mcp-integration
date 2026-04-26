"""T10 integration: nanobrain ApprovalStep against a live Control Plane.

These tests drive the real ``ApprovalStep`` class from
``nanobrain/library/steps/approval_step.py`` against a real FastAPI
Control Plane (in-process via ``httpx.ASGITransport``) backed by a
migrated SQLite DB. No mocks anywhere in the step's HTTP path.

The mock-parity bug this suite exists to catch: the step was
originally reading ``response.json()["id"]`` while the TX1 Control
Plane returns ``{"approval": {"id": ...}}``. Unit tests passed
because the fake matched the step, not the real API. Integration
tests here fail-loud if the contract drifts again.

Coverage (T10 ACs from implementation_plan.md):
- AC1: step pauses, approval row persisted as PENDING.
- AC3: approve via MCP → step resumes with pass-through.
- AC4: correct via MCP → step resumes with modifications merged.
- AC5: reject via MCP → step raises StepRejected with reviewer's
  comment as reason.
- AC6: soft gate with timeout → step applies on_timeout locally.

Out of scope here:
- AC2 (kill server mid-pause): durable state is the CP's; the step's
  ``resume_approval_id`` re-entry is tested, full-process kill is a
  separate harness.
- Soft-gate CP-side reaper parity: no reaper exists yet.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from nanobrain.library.steps.approval_step import ApprovalStep, StepRejected
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_step(
    app: FastAPI,
    tmp_path: Path,
    *,
    kind: str = "hard",
    timeout_seconds: float | None = None,
    on_timeout: str = "reject",
    poll_interval_seconds: float = 0.05,
) -> ApprovalStep:
    """Build an ApprovalStep wired to talk to ``app`` via ASGI transport.

    Nanobrain's ``from_config`` rejects dicts — it requires a YAML file
    path. We write the config to ``tmp_path/approval_config_<uuid>.yml``
    per call so parallel tests don't collide. Then we override
    ``_http_client_factory`` so the step's AsyncClient reaches the
    in-process FastAPI app instead of an external URL.
    """
    config_dict = {
        "name": f"test_approval_gate_{uuid.uuid4().hex[:8]}",
        "description": "integration test",
        "gate_policy": {
            "kind": kind,
            "timeout_seconds": timeout_seconds,
            "on_timeout": on_timeout,
        },
        "control_plane": {
            "base_url": "http://testserver",
            "poll_interval_seconds": poll_interval_seconds,
            "request_timeout_seconds": 5.0,
        },
    }
    config_path = tmp_path / f"approval_config_{uuid.uuid4().hex[:8]}.yml"
    config_path.write_text(yaml.safe_dump(config_dict))
    step = ApprovalStep.from_config(str(config_path))

    def _asgi_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            timeout=5.0,
        )

    step._http_client_factory = _asgi_client_factory
    return step


def _seed_run_and_step(engine: Engine, *, user_id: str = "alex"):
    run_id = uuid.uuid4()
    step_id = uuid.uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, 'PENDING', :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, status, "
                "input_artifact_ids, output_artifact_ids, created_at) "
                "VALUES (:id, :rid, 'approval', 'LOCAL', 'PAUSED_FOR_APPROVAL', "
                "'[]', '[]', :ts)"
            ),
            {"id": str(step_id), "rid": str(run_id), "ts": now},
        )
    return run_id, step_id


async def _async_client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=5.0,
    )


async def _wait_for_pending(engine: Engine, run_id: uuid.UUID, *, timeout: float = 2.0) -> str:
    """Poll the DB until a PENDING approval appears for ``run_id``.

    The decider task uses this to wait for the step to have POSTed,
    so we can then decide without racing.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT approval.id FROM approval "
                    "JOIN step ON approval.step_id = step.id "
                    "WHERE step.run_id = :rid AND approval.status = 'PENDING' "
                    "ORDER BY approval.id LIMIT 1"
                ),
                {"rid": str(run_id)},
            ).scalar()
        if row is not None:
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"no PENDING approval for run_id={run_id} within {timeout}s")


async def test_ac3_approve_causes_step_to_return_pass_through(cp_client, cp_engine, tmp_path):
    """AC3: approve decision → step returns input_data unchanged."""
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_step(app, tmp_path, kind="hard")
    run_id, step_id = _seed_run_and_step(cp_engine)

    input_data = {"proposals": ["A", "B"], "count": 2}

    async def decider():
        approval_id = await _wait_for_pending(cp_engine, run_id)
        async with await _async_client_for(app) as c:
            r = await c.post(
                "/approvals/approve",
                json={"approval_id": str(approval_id), "decided_by": "alex"},
            )
            r.raise_for_status()

    step_result, _ = await asyncio.gather(
        step.process(input_data, run_id=str(run_id), step_id=str(step_id)),
        decider(),
    )
    assert step_result == input_data


async def test_ac4_correct_merges_modifications_into_input(cp_client, cp_engine, tmp_path):
    """AC4: correct decision → modifications shallow-merged into input_data."""
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_step(app, tmp_path, kind="hard")
    run_id, step_id = _seed_run_and_step(cp_engine)

    input_data = {"proposals": ["A", "B"], "count": 2}
    modifications = {"proposals": ["A-corrected"], "note": "trimmed"}

    async def decider():
        approval_id = await _wait_for_pending(cp_engine, run_id)
        async with await _async_client_for(app) as c:
            r = await c.post(
                "/approvals/correct",
                json={
                    "approval_id": str(approval_id),
                    "modifications": modifications,
                    "decided_by": "alex",
                },
            )
            r.raise_for_status()

    step_result, _ = await asyncio.gather(
        step.process(input_data, run_id=str(run_id), step_id=str(step_id)),
        decider(),
    )
    # Modifications shallow-merge: count survives, proposals is replaced,
    # note is added.
    assert step_result["count"] == 2
    assert step_result["proposals"] == ["A-corrected"]
    assert step_result["note"] == "trimmed"


async def test_ac5_reject_raises_step_rejected_with_comment(cp_client, cp_engine, tmp_path):
    """AC5: reject decision → StepRejected with reviewer's reason."""
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_step(app, tmp_path, kind="hard")
    run_id, step_id = _seed_run_and_step(cp_engine)

    reason = "proposals include wrong pathogen"

    async def decider():
        approval_id = await _wait_for_pending(cp_engine, run_id)
        async with await _async_client_for(app) as c:
            r = await c.post(
                "/approvals/reject",
                json={
                    "approval_id": str(approval_id),
                    "reason": reason,
                    "decided_by": "alex",
                },
            )
            r.raise_for_status()

    with pytest.raises(StepRejected) as exc:
        await asyncio.gather(
            step.process({"x": 1}, run_id=str(run_id), step_id=str(step_id)),
            decider(),
        )
    assert reason in str(exc.value)


async def test_ac6_soft_gate_auto_approves_on_timeout(cp_client, cp_engine, tmp_path):
    """AC6 (partial): soft gate with on_timeout=auto_approve returns
    pass-through locally after timeout; no decision is posted.

    Brutal-truth caveat: the Control Plane does NOT (yet) reap pending
    approvals on its own, so the row stays PENDING in the DB even after
    the step returns. This is the documented soft-gate-parity gap — see
    docs/future_work.md. The test here only verifies the LOCAL behavior.
    """
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_step(
        app,
        tmp_path,
        kind="soft",
        timeout_seconds=0.1,
        on_timeout="auto_approve",
        poll_interval_seconds=0.02,
    )
    run_id, step_id = _seed_run_and_step(cp_engine)
    input_data = {"x": 1}

    # No decider: the step must time out and apply on_timeout.
    result = await step.process(input_data, run_id=str(run_id), step_id=str(step_id))
    assert result == input_data

    # CP-side row is still PENDING (reaper not implemented).
    with cp_engine.connect() as conn:
        status_row = conn.execute(
            text(
                "SELECT approval.status FROM approval "
                "JOIN step ON approval.step_id = step.id "
                "WHERE step.run_id = :rid LIMIT 1"
            ),
            {"rid": str(run_id)},
        ).scalar()
    assert status_row == "PENDING"


async def test_ac6_soft_gate_rejects_on_timeout(cp_client, cp_engine, tmp_path):
    """AC6 (partial): soft gate with on_timeout=reject raises StepRejected
    after the local timer fires, even with no decider.
    """
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_step(
        app,
        tmp_path,
        kind="soft",
        timeout_seconds=0.1,
        on_timeout="reject",
        poll_interval_seconds=0.02,
    )
    run_id, step_id = _seed_run_and_step(cp_engine)

    with pytest.raises(StepRejected):
        await step.process({"x": 1}, run_id=str(run_id), step_id=str(step_id))


async def test_resume_approval_id_skips_post_and_polls_existing(cp_client, cp_engine, tmp_path):
    """Restart-recovery shape: passing ``resume_approval_id`` skips the
    POST and polls the existing row. Proves AC2's re-entry contract even
    though we don't kill a real process.
    """
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    run_id, step_id = _seed_run_and_step(cp_engine)

    # Manually POST a pending approval (simulating task A before it died).
    async with await _async_client_for(app) as c:
        r = await c.post(
            "/approvals/",
            json={
                "run_id": str(run_id),
                "step_id": str(step_id),
                "kind": "hard",
                "summary": "pre-existing",
                "artifact_ids": [],
                "policy": {},
            },
        )
        r.raise_for_status()
        approval_id = r.json()["approval"]["id"]

    # Task B re-enters with resume_approval_id and a decider task approves.
    step = _build_step(app, tmp_path, kind="hard")

    async def decider():
        # Brief sleep so the step starts its poll loop before we decide.
        await asyncio.sleep(0.02)
        async with await _async_client_for(app) as c:
            r = await c.post(
                "/approvals/approve",
                json={"approval_id": approval_id, "decided_by": "alex"},
            )
            r.raise_for_status()

    step_result, _ = await asyncio.gather(
        step.process(
            {"payload": True},
            run_id=str(run_id),
            step_id=str(step_id),
            resume_approval_id=approval_id,
        ),
        decider(),
    )
    assert step_result == {"payload": True}

    # Only ONE approval exists for this run — proves the step did not
    # POST a second one.
    with cp_engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM approval "
                "JOIN step ON approval.step_id = step.id "
                "WHERE step.run_id = :rid"
            ),
            {"rid": str(run_id)},
        ).scalar()
    assert n == 1
