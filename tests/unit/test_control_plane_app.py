"""Unit tests for the serve-time wiring helpers in ``control_plane.app``.

Covers the module-level pieces the ``run-sweeper-serve-wiring`` fix added so
the RunStateSweeper is actually invoked while serving:

- ``_resolve_sweep_interval`` — env parsing + safe fallback.
- ``_run_sweep_loop`` — the periodic coroutine that calls ``sweeper.sweep``.

No external dependency is mocked here: ``_run_sweep_loop`` is driven with a
plain in-process stub sweeper and a REAL asyncio event loop. The end-to-end
behaviour against the real sweeper + real migrated DB + real app lifespan is
covered by ``tests/integration/test_serve_sweeps_stale_runs.py``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from fastapi.testclient import TestClient

from apecx_integration.control_plane import app


class _SpySweeper:
    """Records the ``stale_after`` of every ``sweep`` call. Returns [] (no
    reaped runs) so the loop takes the no-log branch."""

    def __init__(self) -> None:
        self.calls: list[timedelta] = []

    def sweep(self, *, stale_after):
        self.calls.append(stale_after)
        return []


def test_run_sweep_loop_invokes_sweep() -> None:
    spy = _SpySweeper()
    stale_after = timedelta(minutes=15)

    async def _drive() -> None:
        task = asyncio.create_task(
            app._run_sweep_loop(spy, interval_seconds=0.01, stale_after=stale_after)
        )
        # Enough real time for several 0.01s iterations.
        await asyncio.sleep(0.1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(_drive())

    assert len(spy.calls) >= 1
    assert all(recorded == stale_after for recorded in spy.calls)


def test_resolve_sweep_interval_env(monkeypatch) -> None:
    # Default when unset.
    monkeypatch.delenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", raising=False)
    assert app._resolve_sweep_interval() == 300.0

    # Explicit override wins.
    monkeypatch.setenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", "0.2")
    assert app._resolve_sweep_interval() == 0.2

    # Un-parseable value falls back to the default (must not break serve).
    monkeypatch.setenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", "not-a-number")
    assert app._resolve_sweep_interval() == 300.0

    # Non-positive value falls back to the default.
    monkeypatch.setenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", "-5")
    assert app._resolve_sweep_interval() == 300.0

    # Non-finite (inf/nan) must NOT silently disable the reaper (sleep(inf) never wakes).
    monkeypatch.setenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", "inf")
    assert app._resolve_sweep_interval() == 300.0
    monkeypatch.setenv("APECX_RUN_SWEEP_INTERVAL_SECONDS", "nan")
    assert app._resolve_sweep_interval() == 300.0


# --- request-id / access-log middleware (GAP2) -------------------------------


def test_request_id_minted_and_echoed():
    """Every response carries an X-Request-ID header (minted when absent)."""
    client = TestClient(app.create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID")  # non-empty


def test_request_id_propagated_from_inbound_header():
    """An inbound X-Request-ID flows through to the response (caller correlation)."""
    client = TestClient(app.create_app())
    resp = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert resp.headers.get("X-Request-ID") == "abc123"


def test_request_id_inbound_is_sanitized():
    """A caller-supplied id with newlines/control chars is stripped (log-forging guard);
    an all-invalid id falls back to a freshly minted one."""
    client = TestClient(app.create_app())
    resp = client.get("/healthz", headers={"X-Request-ID": "ab\r\ncd evil"})
    echoed = resp.headers.get("X-Request-ID")
    assert echoed == "abcdevil"  # newline/CR/space removed
    assert "\n" not in echoed and "\r" not in echoed


def test_sanitize_request_id_strips_unicode_to_ascii():
    """_sanitize_request_id must drop Unicode-alnum chars (bare isalnum() admits them, which
    then blows up latin-1 RESPONSE-header encoding → a caller-controlled 500). Tested directly:
    httpx refuses to SEND a non-latin-1 header, so this guards the server-side encode path that a
    raw client could still reach. Regression guard for the review-gate finding."""
    # Mixed unicode+ascii → only ascii-alnum/-/_ survive, result is ascii-encodable.
    out = app._sanitize_request_id("你好abc-1")
    assert out == "abc-1"
    assert out.isascii()
    # Entirely-unicode → all stripped → a freshly minted ascii id (never empty, never unicode).
    out2 = app._sanitize_request_id("你好世界")
    assert out2 and out2.isascii()
    # Control chars / CR-LF / spaces stripped (log-forging / header-splitting).
    assert app._sanitize_request_id("a\r\nb c") == "abc"
    # Length cap.
    assert len(app._sanitize_request_id("x" * 200)) == 64


def test_access_line_logged_with_rid(caplog):
    """A structured cp-access line is logged per request, carrying the rid + method/path/status."""
    client = TestClient(app.create_app())
    with caplog.at_level(logging.INFO, logger=app.log.name):
        client.get("/healthz", headers={"X-Request-ID": "trace9"})
    lines = [r.getMessage() for r in caplog.records if "cp-access" in r.getMessage()]
    assert any("rid=trace9" in m and "GET /healthz -> 200" in m for m in lines), lines
