"""W3 — InfraMonitor tick logic (fake orchestrator; deterministic).

Real parity: tests/integration/test_infra_monitor_real.py drives the daemon against a real docker
container (reload a stopped backend + record)."""

from __future__ import annotations

import asyncio

from apecx_integration.infrastructure.failure_log import InfraFailureLog
from apecx_integration.infrastructure.monitor import InfraMonitor


class _FakeOrch:
    def __init__(self, snapshots):
        self._snaps = list(snapshots)
        self.reloaded = []

    async def status(self):
        s = self._snaps[0]
        if len(self._snaps) > 1:
            self._snaps.pop(0)
        return s

    async def reload_backend(self, name):
        self.reloaded.append(name)
        return {"name": name, "state": "ready"}


def _backend(name, state, kind="docker_container", detail="", reachable=False):
    # reachable defaults False (a red backend is genuinely down); the up-but-degraded case sets it True.
    return {"name": name, "state": state, "kind": kind, "detail": detail, "reachable": reachable}


def _monitor(tmp_path, orch, **kw):
    return InfraMonitor(orchestrator=orch, failure_log=InfraFailureLog(tmp_path / "f.jsonl"), **kw)


def test_tick_reloads_red_reloadable_backend_and_records(tmp_path):
    orch = _FakeOrch([{"backends": [_backend("redis", "down")]}])
    m = _monitor(tmp_path, orch)
    asyncio.run(m.tick(monotonic=1000.0))
    assert orch.reloaded == ["redis"]
    rec = m.recent_failures()
    assert rec[-1]["component"] == "redis" and rec[-1]["reload_attempted"] is True
    assert "reload" in rec[-1]["reload_outcome"]


def test_tick_records_but_does_not_reload_degraded(tmp_path):
    # DEGRADED but REACHABLE means the backend is UP (e.g. Ollama serving with no model) — no restart.
    orch = _FakeOrch(
        [{"backends": [_backend("ollama", "degraded", detail="no model", reachable=True)]}]
    )
    m = _monitor(tmp_path, orch)
    asyncio.run(m.tick(monotonic=1000.0))
    assert orch.reloaded == []
    assert m.recent_failures()[-1]["component"] == "ollama"


def test_tick_does_not_reload_external_backend(tmp_path):
    orch = _FakeOrch([{"backends": [_backend("ollama", "down", kind="external")]}])
    m = _monitor(tmp_path, orch)
    asyncio.run(m.tick(monotonic=1000.0))
    assert orch.reloaded == []  # external endpoint is operator-owned


def test_operator_prereq_states_recorded_not_reloaded(tmp_path):
    # external_missing (docker daemon down) / external_unconfigured (host prereq unset) can't be fixed
    # by a reload — record them, don't thrash a restart every backoff (review-gate W2).
    orch = _FakeOrch([{"backends": [_backend("postgres", "external_missing", reachable=False)]}])
    m = _monitor(tmp_path, orch, backoff_s=0.0)
    asyncio.run(m.tick(monotonic=1000.0))
    assert orch.reloaded == []
    assert m.recent_failures()[-1]["component"] == "postgres"


def test_backoff_prevents_restart_storm(tmp_path):
    orch = _FakeOrch([{"backends": [_backend("redis", "down")]}])  # always down
    m = _monitor(tmp_path, orch, backoff_s=60.0)
    asyncio.run(m.tick(monotonic=1000.0))  # reload
    asyncio.run(m.tick(monotonic=1030.0))  # within backoff → skip
    asyncio.run(m.tick(monotonic=1100.0))  # past backoff → reload
    assert orch.reloaded == ["redis", "redis"]


def test_records_on_change_not_every_tick(tmp_path):
    orch = _FakeOrch([{"backends": [_backend("redis", "down")]}])  # unchanging down
    m = _monitor(tmp_path, orch, backoff_s=1e9)  # backoff blocks re-reload → attempted only once
    asyncio.run(m.tick(monotonic=1000.0))  # state-change into down + reload → 1 record
    asyncio.run(m.tick(monotonic=1001.0))  # same state, no reload → NO record
    asyncio.run(m.tick(monotonic=1002.0))  # same → NO record
    assert len(m.recent_failures()) == 1


def test_healthy_backend_produces_no_record(tmp_path):
    orch = _FakeOrch([{"backends": [_backend("redis", "ready"), _backend("minio", "reused")]}])
    m = _monitor(tmp_path, orch)
    asyncio.run(m.tick(monotonic=1000.0))
    assert orch.reloaded == [] and m.recent_failures() == []


def test_recent_buffer_is_bounded(tmp_path):
    orch = _FakeOrch([{"backends": [_backend("redis", "down")]}])
    m = _monitor(tmp_path, orch, backoff_s=0.0, recent_maxlen=3)
    for i in range(10):
        asyncio.run(m.tick(monotonic=float(i)))  # each tick reloads (backoff 0) → records
    assert len(m.recent_failures()) == 3  # deque maxlen
