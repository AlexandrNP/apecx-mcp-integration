"""#7 Part 4 — model-aware ``ollama_probe`` against a LIVE Ollama daemon (real coverage / parity
partner of tests/unit/test_ollama_probe_model_aware.py).

Auto-skips when ``APECX_SKIP_LIVE_LLM=1`` or no reachable Ollama with >=1 model (mirrors the
``*_against_ollama`` suite gate). Uses a model that is ACTUALLY installed (read from /api/tags) so the
healthy assertion doesn't depend on which specific model is configured."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from apecx_integration.infrastructure.probes import ollama_probe

pytestmark = pytest.mark.integration

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def _installed_models() -> list[str]:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        if r.status_code == 200:
            return [m.get("name", "") for m in (r.json().get("models") or [])]
    except Exception:  # noqa: BLE001 — unreachable daemon → skip, not error
        return []
    return []


_MODELS = _installed_models()
_SKIP = os.environ.get("APECX_SKIP_LIVE_LLM") == "1" or not _MODELS
_REASON = (
    "APECX_SKIP_LIVE_LLM=1"
    if os.environ.get("APECX_SKIP_LIVE_LLM") == "1"
    else "no reachable Ollama with >=1 installed model"
)


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_installed_model_reads_healthy():
    r = asyncio.run(ollama_probe(base_url=OLLAMA_URL, required_model=_MODELS[0]))
    assert r.healthy, r.detail


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_absent_model_reads_unhealthy_with_pull_hint():
    r = asyncio.run(ollama_probe(base_url=OLLAMA_URL, required_model="apecx-nonexistent-model:0b"))
    assert not r.healthy
    assert "ollama pull apecx-nonexistent-model:0b" in r.detail


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_no_required_model_floor_is_healthy_when_models_present():
    r = asyncio.run(ollama_probe(base_url=OLLAMA_URL, required_model=None))
    assert r.healthy, r.detail


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_setup_http_pull_of_present_model_succeeds(monkeypatch):
    # #7 Slice B.2 — the container-aware HTTP pull (POST /api/pull) against a REAL Ollama. Pulling an
    # already-installed model verifies the code path end-to-end without a multi-GB download.
    from apecx_integration.cli import setup

    monkeypatch.setenv("APECX_LLM_BASE_URL", OLLAMA_URL)
    ok, msg = setup._pull_ollama_model_http(_MODELS[0])
    assert ok, msg
    assert _MODELS[0] in msg
