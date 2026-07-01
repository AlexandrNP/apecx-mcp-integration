"""#7 Slice B real coverage — the orchestrator brings up a REAL model-less ollama container and reads
it DEGRADED (up, needs `apecx-setup llm`), NOT ERROR_STARTING. This is the honest-state contract: a
live-but-unprovisioned container must never be mislabelled as a start failure.

Docker-gated; auto-skips when docker is unavailable. Uses a throwaway container on a free port so it
never collides with a host/real apecx-ollama."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from apecx_integration.infrastructure.backends import (
    BackendSpec,
    BackendState,
    ContainerSpec,
    Probe,
)
from apecx_integration.infrastructure.orchestrator import InfraOrchestrator
from apecx_integration.infrastructure.probes import ollama_probe

pytestmark = pytest.mark.integration

_DOCKER = shutil.which("docker")
_PORT = 11599
_NAME = "apecx-ollama-slicetest"


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
def _throwaway_container():
    _rm()  # in case a prior run left one
    yield
    _rm()


@pytest.mark.skipif(_SKIP, reason="docker not available")
def test_orchestrator_reads_modelless_ollama_container_as_degraded(_throwaway_container):
    container = ContainerSpec(
        image="ollama/ollama",
        container_name=_NAME,
        ports=((_PORT, 11434),),
        volumes=(),
        ready_timeout_s=15.0,  # > container-startup; the model-aware poll never goes healthy
    )

    async def _probe():
        return await ollama_probe(
            base_url=f"http://localhost:{_PORT}", required_model="apecx-nonexistent-model:0b"
        )

    spec = BackendSpec(
        name="ollama",
        display_name="Ollama (container test)",
        kind="docker_container",
        required=False,
        probe=Probe(name="ollama", fn=_probe),
        container=container,
        actionable_message="run apecx-setup llm",
    )
    orch = InfraOrchestrator(specs=[spec], autostart_enabled=True, docker_binary=_DOCKER)

    asyncio.run(orch.start_all())
    rt = orch.get_runtime("ollama")
    assert rt.state == BackendState.DEGRADED, f"expected DEGRADED, got {rt.state}: {rt.detail}"
    assert "model" in rt.detail.lower()
