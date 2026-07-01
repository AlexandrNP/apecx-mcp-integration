"""#7 Slice B (2026-07-01) — Ollama flips to a managed CONTAINER for a LOCAL endpoint (a remote
APECX_LLM_BASE_URL stays probe-only external), plus ProbeResult.reachable so a spawned-but-model-less
container reads DEGRADED (up, needs `apecx-setup llm`) rather than ERROR_STARTING.

Deterministic coverage (no docker). The real container-spawn → DEGRADED behaviour is verified in
tests/integration/test_ollama_container_real.py (docker-gated)."""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.infrastructure import orchestrator as orch
from apecx_integration.infrastructure.backends import ProbeResult
from apecx_integration.infrastructure.containers import APECX_OLLAMA, all_container_specs
from apecx_integration.infrastructure.probes import ollama_probe


def test_apecx_ollama_registered_and_shaped():
    assert APECX_OLLAMA in all_container_specs()
    assert APECX_OLLAMA.image == "ollama/ollama"
    assert APECX_OLLAMA.container_name == "apecx-ollama"
    assert (11434, 11434) in APECX_OLLAMA.ports
    # Named volume so pulled models survive respawn (else every restart re-downloads GBs).
    assert APECX_OLLAMA.volumes == (("apecx-ollama-data", "/root/.ollama"),)


@pytest.mark.parametrize(
    "url",
    [None, "http://localhost:11434/v1", "http://127.0.0.1:11434/v1", "http://0.0.0.0:11434"],
)
def test_local_endpoint_is_docker_container(monkeypatch, url):
    if url is None:
        monkeypatch.delenv("APECX_LLM_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("APECX_LLM_BASE_URL", url)
    s = orch._make_ollama_spec()
    assert s.kind == "docker_container"
    assert s.container is APECX_OLLAMA  # __post_init__ would raise if container were None


def test_remote_endpoint_stays_external(monkeypatch):
    monkeypatch.setenv("APECX_LLM_BASE_URL", "http://gpu-box.internal:11434/v1")
    s = orch._make_ollama_spec()
    assert s.kind == "external"
    assert s.container is None  # __post_init__ would raise if a container were set on external


def test_probe_result_reachable_defaults_true():
    assert ProbeResult(healthy=True, detail="x", latency_ms=1.0).reachable is True


def test_ollama_probe_unreachable_sets_reachable_false():
    # A dead port → connection refused → reachable=False (bring-up would map this to ERROR_STARTING,
    # NOT the DEGRADED "up but no model" path).
    r = asyncio.run(ollama_probe(base_url="http://localhost:1", required_model=None, timeout_s=2.0))
    assert not r.healthy
    assert r.reachable is False


def test_infra_probes_set_reachable_false_when_unreachable():
    # #7 review-gate fix: a genuinely-DOWN postgres/redis/minio container must set reachable=False on
    # its connection-failure path, so the SHARED _bring_up_container branch reads ERROR_STARTING, not
    # DEGRADED ("container up") — reachable defaults True, so an unset failure path would mislabel.
    from apecx_integration.infrastructure.probes import minio_probe, postgres_probe, redis_probe

    coros = [
        postgres_probe(host="localhost", port=1, user="x", db="x", password="x", timeout_s=2.0),
        redis_probe(host="localhost", port=1, timeout_s=2.0),
        minio_probe(host="localhost", port=1, timeout_s=2.0),
    ]
    for coro in coros:
        r = asyncio.run(coro)
        assert not r.healthy and r.reachable is False, r
