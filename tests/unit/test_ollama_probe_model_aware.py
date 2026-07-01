"""#7 Part 4 (2026-07-01) — ollama_probe is MODEL-AWARE: a reachable Ollama that lacks the required
model (or has 0 models) reads unhealthy with an actionable `ollama pull` hint, instead of green-on-
connectivity (the silent-failure trap that deferred the ollama-as-container work).

Unit-level: mock httpx to feed canned /api/tags payloads so the branch logic is deterministic. The
matching REAL coverage is tests/integration/test_ollama_model_readiness_against_ollama.py (parity)."""

from __future__ import annotations

import asyncio

from apecx_integration.infrastructure import probes
from apecx_integration.infrastructure.probes import _model_present, ollama_probe


def _patch_tags(monkeypatch, payload, status=200):
    class _Resp:
        status_code = status

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url):
            return _Resp()

    monkeypatch.setattr(probes.httpx, "AsyncClient", _Client)


def test_model_present_is_tag_tolerant():
    assert _model_present(["mistral-nemo:latest"], "mistral-nemo")
    assert _model_present(["mistral-nemo"], "mistral-nemo:latest")
    assert _model_present(["x:1", "nemotron-3-nano:4b"], "nemotron-3-nano:4b")
    assert not _model_present(["x:1"], "b")
    assert not _model_present([], "b")


def test_required_model_present_is_healthy(monkeypatch):
    _patch_tags(monkeypatch, {"models": [{"name": "nemotron-3-nano:4b"}, {"name": "other:1"}]})
    r = asyncio.run(ollama_probe(base_url="http://x:11434", required_model="nemotron-3-nano:4b"))
    assert r.healthy
    assert "2 model(s) loaded" in r.detail


def test_required_model_missing_is_unhealthy_with_pull_hint(monkeypatch):
    _patch_tags(monkeypatch, {"models": [{"name": "other:1"}]})
    r = asyncio.run(ollama_probe(base_url="http://x:11434", required_model="nemotron-3-nano:4b"))
    assert not r.healthy
    assert "ollama pull nemotron-3-nano:4b" in r.detail
    assert "missing" in (r.error or "")


def test_zero_models_is_unhealthy_floor(monkeypatch):
    _patch_tags(monkeypatch, {"models": []})
    r = asyncio.run(ollama_probe(base_url="http://x:11434", required_model=None))
    assert not r.healthy
    assert "0 models" in r.detail


def test_models_present_without_required_is_healthy(monkeypatch):
    _patch_tags(monkeypatch, {"models": [{"name": "any:1"}]})
    r = asyncio.run(ollama_probe(base_url="http://x:11434", required_model=None))
    assert r.healthy


def test_non_200_is_unhealthy(monkeypatch):
    _patch_tags(monkeypatch, None, status=503)
    r = asyncio.run(ollama_probe(base_url="http://x:11434", required_model=None))
    assert not r.healthy
    assert "503" in (r.error or "") + r.detail
