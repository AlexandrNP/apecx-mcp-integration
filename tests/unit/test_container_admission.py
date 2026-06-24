"""Unit tests for the host-side code-exec container admission cap.

Pins the open-endpoint exhaustion guard (deployment-hardening Task A): the shared
semaphore caps how many ``docker run`` containers run simultaneously process-wide, is
configured by ``APECX_MAX_CONCURRENT_DOCKER_RUNS``, fails loud on bad values, and
rebinds across event loops. The matching real-spawn coverage (the unit-mock /
integration-parity rule) lives in ``tests/integration/test_docker_sandbox_runtime.py``
(``test_admission_cap_serializes_concurrent_spawns``, docker-gated; plus
``test_spawn_failure_releases_admission_slot``, which pins the no-leak-on-spawn-failure
path without docker).
"""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.composition.runtime import container_admission as ca


@pytest.fixture(autouse=True)
def _reset_admission_state():
    # The semaphore is cached process-wide; reset so each test re-reads the env var.
    ca._reset_for_test()
    yield
    ca._reset_for_test()


def _observe_max_concurrency(workers: int, hold_seconds: float = 0.05) -> int:
    """Run ``workers`` coroutines that each hold a slot for ``hold_seconds``; return
    the peak number that held a slot at the same time."""
    state = {"current": 0, "peak": 0}

    async def worker() -> None:
        async with ca.acquire_container_slot():
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            await asyncio.sleep(hold_seconds)
            state["current"] -= 1

    async def main() -> None:
        await asyncio.gather(*(worker() for _ in range(workers)))

    asyncio.run(main())
    return state["peak"]


def test_caps_concurrency_from_env(monkeypatch):
    # cap=2, 5 contenders -> never more than 2 hold a slot at once.
    monkeypatch.setenv(ca.ENV_VAR, "2")
    ca._reset_for_test()
    assert _observe_max_concurrency(workers=5) == 2


def test_caps_concurrency_default(monkeypatch):
    # No env -> the built-in default cap bounds 6 contenders.
    monkeypatch.delenv(ca.ENV_VAR, raising=False)
    ca._reset_for_test()
    assert _observe_max_concurrency(workers=6) == ca._DEFAULT_MAX


@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_invalid_env_fails_loud(monkeypatch, bad):
    # A misconfigured cap must raise, not silently fall back to a default.
    monkeypatch.setenv(ca.ENV_VAR, bad)
    ca._reset_for_test()

    async def main() -> None:
        ca.acquire_container_slot()

    with pytest.raises(ValueError):
        asyncio.run(main())


def test_rebinds_on_new_loop(monkeypatch):
    # asyncio.Semaphore binds to its creating loop; a second asyncio.run() (a fresh
    # loop) must rebind, not raise "bound to a different event loop".
    monkeypatch.setenv(ca.ENV_VAR, "2")
    ca._reset_for_test()

    async def acquire_and_release() -> None:
        async with ca.acquire_container_slot():
            await asyncio.sleep(0)

    asyncio.run(acquire_and_release())  # binds to loop #1
    asyncio.run(acquire_and_release())  # loop #2 — must not raise
