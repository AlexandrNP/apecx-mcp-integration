"""E3-6 — single source of truth for the synthesis model + loud preflight.

Guards the three-way model-default divergence (factory / installer / composer)
that produced cryptic Ollama 404s on a fresh install, and the preflight that
turns an un-pulled model into a clear `ollama pull <model>` error.

The live counterparts run against a real Ollama in
``tests/integration/test_llm_preflight_against_ollama.py`` (nemotron-3-nano:4b
passes; a bogus model fails loud).
"""

from __future__ import annotations

import logging

import pytest
import requests

from apecx_integration.agents import _llm_config
from apecx_integration.agents._llm_config import (
    DEFAULT_LLM_MODEL,
    preflight_llm_model,
    resolve_llm_model,
)
from apecx_integration.cli import setup


@pytest.fixture(autouse=True)
def _clear_preflight_cache():
    # The preflight caches per process; clear it so each test probes fresh.
    _llm_config._preflight_done.clear()
    yield
    _llm_config._preflight_done.clear()


class _FakeResponse:
    def __init__(self, payload: dict, ok: bool = True):
        self._payload = payload
        self.ok = ok

    def json(self) -> dict:
        return self._payload


def _tags_with(*names: str):
    """A fake /api/tags responder over the given model names."""

    def _get(url: str, timeout: float = 3):
        return _FakeResponse({"models": [{"name": n} for n in names]})

    return _get


# --- resolver: single source of truth -------------------------------------


def test_resolve_returns_nonempty_default(monkeypatch):
    monkeypatch.delenv("APECX_LLM_MODEL", raising=False)
    assert resolve_llm_model() == DEFAULT_LLM_MODEL
    assert resolve_llm_model()  # non-empty (CC-1)


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("APECX_LLM_MODEL", "llama4:latest")
    assert resolve_llm_model() == "llama4:latest"


def test_empty_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APECX_LLM_MODEL", "")
    assert resolve_llm_model() == DEFAULT_LLM_MODEL


def test_installer_and_resolver_no_longer_diverge(monkeypatch):
    # The root bug: the installer pulled one model and the runtime asked for
    # another. They must now be the SAME string — both at the default and
    # under an env override.
    monkeypatch.delenv("APECX_LLM_MODEL", raising=False)
    assert setup._ollama_model() == resolve_llm_model() == DEFAULT_LLM_MODEL

    monkeypatch.setenv("APECX_LLM_MODEL", "mixtral:latest")
    assert setup._ollama_model() == resolve_llm_model() == "mixtral:latest"


# --- preflight ------------------------------------------------------------


def test_preflight_passes_when_model_pulled(monkeypatch):
    monkeypatch.setattr(requests, "get", _tags_with("nemotron-3-nano:4b"))
    preflight_llm_model(model="nemotron-3-nano:4b", base_url="http://x:11434/v1")


def test_preflight_raises_naming_pull_command_when_unpulled(monkeypatch):
    monkeypatch.setattr(requests, "get", _tags_with("some-other-model:latest"))

    with pytest.raises(RuntimeError) as exc:
        preflight_llm_model(model="ghost-model:9b", base_url="http://x:11434/v1")

    message = str(exc.value)
    assert message  # non-empty (CC-1)
    assert "ollama pull ghost-model:9b" in message
    assert "APECX_LLM_MODEL" in message


def test_preflight_unreachable_warns_does_not_raise(monkeypatch, caplog):
    def _refuse(url: str, timeout: float = 3):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", _refuse)

    with caplog.at_level(logging.WARNING):
        preflight_llm_model(model="nemotron-3-nano:4b", base_url="http://down:11434/v1")

    assert any("unreachable" in r.message for r in caplog.records)


def test_preflight_probes_once_per_process(monkeypatch):
    calls = {"n": 0}

    def _counting_get(url: str, timeout: float = 3):
        calls["n"] += 1
        return _FakeResponse({"models": [{"name": "nemotron-3-nano:4b"}]})

    monkeypatch.setattr(requests, "get", _counting_get)
    preflight_llm_model(model="nemotron-3-nano:4b", base_url="http://x:11434/v1")
    first = calls["n"]
    preflight_llm_model(model="nemotron-3-nano:4b", base_url="http://x:11434/v1")
    assert calls["n"] == first  # cached: no second probe
