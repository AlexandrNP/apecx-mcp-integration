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
from contextlib import suppress
from datetime import timedelta

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
