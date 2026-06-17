"""InfraOrchestrator.reconcile() — self-heal docker backends when Docker comes up post-startup.

Docker is detected once at construction + once per backend during the initial start_all(); if the
user starts the daemon LATER, the docker backends would stay stuck. reconcile() (called from the
tool seams) re-attempts bring-up when a docker backend is stuck AND `docker info` is now green,
throttled so a persistently-down daemon isn't hammered.
"""

from __future__ import annotations

import asyncio
import types

from apecx_integration.infrastructure import orchestrator as orch_mod
from apecx_integration.infrastructure.backends import BackendState
from apecx_integration.infrastructure.orchestrator import InfraOrchestrator


def _orch(docker_binary: str | None = "/fake/docker") -> InfraOrchestrator:
    # Real 5-backend roster (3 docker_container: postgres/redis/minio) with a FAKE docker binary
    # so no real `docker` is invoked; subprocess.run is monkeypatched per test.
    return InfraOrchestrator(autostart_enabled=True, docker_binary=docker_binary)


def _docker_backends(o: InfraOrchestrator):
    return [rt for rt in o._runtimes.values() if rt.spec.kind == "docker_container"]


def _fake_info(returncode: int):
    return lambda *a, **k: types.SimpleNamespace(returncode=returncode, stderr=b"", stdout=b"")


def _patch_start_all(o: InfraOrchestrator, calls: list):
    async def _fake_start_all():
        calls.append(1)
        return {}

    o.start_all = _fake_start_all  # type: ignore[method-assign]


def test_reattempts_when_a_docker_backend_is_stuck_and_daemon_up(monkeypatch):
    o = _orch()
    _docker_backends(o)[0].state = BackendState.EXTERNAL_MISSING  # stuck
    monkeypatch.setattr(orch_mod.subprocess, "run", _fake_info(0))  # daemon up
    calls: list = []
    _patch_start_all(o, calls)

    out = asyncio.run(o.reconcile())
    assert calls == [1], "start_all must be re-attempted when docker came up"
    assert out["reattempted"], "should report the re-attempted backend(s)"


def test_noop_when_daemon_still_down(monkeypatch):
    o = _orch()
    _docker_backends(o)[0].state = BackendState.EXTERNAL_MISSING
    monkeypatch.setattr(orch_mod.subprocess, "run", _fake_info(1))  # daemon still down
    calls: list = []
    _patch_start_all(o, calls)

    out = asyncio.run(o.reconcile())
    assert calls == [], "start_all must NOT run while the daemon is down"
    assert out["reattempted"] == []


def test_noop_when_nothing_stuck_no_docker_info_cost(monkeypatch):
    o = _orch()
    for rt in _docker_backends(o):
        rt.state = BackendState.READY  # all healthy

    # If reconcile pays the docker info cost on the happy path, this raises.
    def _boom(*a, **k):
        raise AssertionError("docker info must NOT run when nothing is stuck")

    monkeypatch.setattr(orch_mod.subprocess, "run", _boom)
    calls: list = []
    _patch_start_all(o, calls)

    out = asyncio.run(o.reconcile())
    assert out["reattempted"] == [] and calls == []


def test_reattempt_is_throttled(monkeypatch):
    o = _orch()
    _docker_backends(o)[0].state = BackendState.EXTERNAL_MISSING
    monkeypatch.setattr(orch_mod.subprocess, "run", _fake_info(0))
    calls: list = []
    _patch_start_all(o, calls)

    asyncio.run(o.reconcile())  # first: re-attempts
    # backend still stuck (fake start_all didn't change it); immediate 2nd call is throttled
    out2 = asyncio.run(o.reconcile())
    assert calls == [1], "second reconcile within the throttle window must NOT re-attempt"
    assert out2.get("throttled") is True


def test_noop_when_no_docker_binary(monkeypatch):
    # Explicit empty binary (None would trigger a real shutil.which at construction, which
    # resolves on a dev machine that has Docker). reconcile re-resolves via shutil.which.
    o = _orch(docker_binary="")
    _docker_backends(o)[0].state = BackendState.EXTERNAL_MISSING
    monkeypatch.setattr(orch_mod.shutil, "which", lambda _name: None)  # still not installed
    monkeypatch.setattr(
        orch_mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no binary → no docker info")),
    )
    calls: list = []
    _patch_start_all(o, calls)

    out = asyncio.run(o.reconcile())
    assert out["reattempted"] == [] and calls == []
