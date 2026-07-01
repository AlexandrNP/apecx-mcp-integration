"""W3 real coverage — the monitor detects a STOPPED real container (unreachable) and auto-reloads it +
records a FailureEvent. Docker-gated; uses a throwaway redis on a free port."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from apecx_integration.infrastructure.backends import BackendSpec, ContainerSpec, Probe
from apecx_integration.infrastructure.failure_log import InfraFailureLog
from apecx_integration.infrastructure.monitor import InfraMonitor
from apecx_integration.infrastructure.orchestrator import InfraOrchestrator
from apecx_integration.infrastructure.probes import redis_probe

pytestmark = pytest.mark.integration

_DOCKER = shutil.which("docker")
_PORT = 6399
_NAME = "apecx-redis-monitortest"


def _docker_up() -> bool:
    if not _DOCKER:
        return False
    try:
        return subprocess.run([_DOCKER, "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


_SKIP = not _docker_up()


def _rm():
    if _DOCKER:
        subprocess.run([_DOCKER, "rm", "-f", _NAME], capture_output=True, timeout=30)


@pytest.fixture
def _throwaway():
    _rm()
    yield
    _rm()


def _redis_spec() -> BackendSpec:
    container = ContainerSpec(
        image="redis:7",
        container_name=_NAME,
        ports=((_PORT, 6379),),
        volumes=(),
        ready_timeout_s=20.0,
    )

    async def _probe():
        return await redis_probe(host="localhost", port=_PORT, timeout_s=3.0)

    return BackendSpec(
        name="redis",
        display_name="Redis (monitor test)",
        kind="docker_container",
        required=False,
        probe=Probe(name="redis", fn=_probe),
        container=container,
        actionable_message="x",
    )


@pytest.mark.skipif(_SKIP, reason="docker not available")
def test_reload_backend_restarts_a_real_stopped_container(_throwaway):
    orch = InfraOrchestrator(specs=[_redis_spec()], autostart_enabled=True, docker_binary=_DOCKER)
    asyncio.run(orch.start_all())
    assert orch.get_runtime("redis").state.value in ("ready", "reused")
    subprocess.run([_DOCKER, "stop", _NAME], capture_output=True, timeout=30)
    result = asyncio.run(orch.reload_backend("redis"))
    assert result["state"] in ("ready", "reused"), result


@pytest.mark.skipif(_SKIP, reason="docker not available")
def test_monitor_detects_stopped_container_reloads_and_records(_throwaway, tmp_path):
    orch = InfraOrchestrator(specs=[_redis_spec()], autostart_enabled=True, docker_binary=_DOCKER)
    asyncio.run(orch.start_all())
    subprocess.run([_DOCKER, "stop", _NAME], capture_output=True, timeout=30)

    log = InfraFailureLog(tmp_path / "f.jsonl")
    m = InfraMonitor(orchestrator=orch, failure_log=log, backoff_s=0.0)
    asyncio.run(m.tick())

    # The tick: status() re-probes the stopped redis → unreachable → the monitor reloads it back up.
    assert orch.get_runtime("redis").state.value in ("ready", "reused"), orch.get_runtime(
        "redis"
    ).detail
    recent = m.recent_failures()
    assert any(r["component"] == "redis" and r["reload_attempted"] for r in recent), recent
    assert log.recent(), "the failure was recorded to the JSONL sink"
