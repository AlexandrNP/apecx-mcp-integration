"""#7 Slice B.2 (2026-07-01) — apecx-setup provisions Ollama container-aware: HTTP /api/pull (no host
`ollama` binary), adopt-or-start the apecx-ollama container, container-aware _probe_llm.

Deterministic (mocked HTTP/docker). Real parity: the live-ollama smoke in the commit body + the gated
tests/integration/test_ollama_model_readiness_against_ollama.py exercise the same code against a real
daemon."""

from __future__ import annotations

import io
import urllib.request
from unittest import mock

import pytest

from apecx_integration.cli import setup


@pytest.fixture(autouse=True)
def _local_by_default(monkeypatch):
    monkeypatch.setenv(
        "APECX_LLM_BASE_URL", ""
    )  # force the local (container) path unless overridden


def test_ensure_serving_adopts_running_ollama(monkeypatch):
    monkeypatch.setattr(setup, "_ollama_reachable", lambda *a, **k: True)
    ok, msg = setup._ensure_ollama_serving()
    assert ok and "adopt" in msg.lower()


def test_ensure_serving_skips_when_no_docker_and_no_host(monkeypatch):
    monkeypatch.setattr(setup, "_ollama_reachable", lambda *a, **k: False)
    monkeypatch.setattr(setup, "_docker_available", lambda: False)
    monkeypatch.setattr(
        setup, "_offer_install_ollama", lambda **k: False
    )  # host fallback unavailable
    ok, msg = setup._ensure_ollama_serving(interactive=False)
    assert not ok and "docker" in msg.lower()


def test_ensure_serving_falls_back_to_host_when_no_docker(monkeypatch):
    reach = iter([False, True])  # unreachable first, reachable after the host daemon starts
    monkeypatch.setattr(setup, "_ollama_reachable", lambda *a, **k: next(reach, True))
    monkeypatch.setattr(setup, "_docker_available", lambda: False)
    monkeypatch.setattr(setup, "_offer_install_ollama", lambda **k: True)
    monkeypatch.setattr(setup, "_offer_start_ollama_daemon", lambda **k: True)
    ok, msg = setup._ensure_ollama_serving(interactive=False)
    assert ok and "host" in msg.lower()


def test_ensure_serving_starts_container_when_absent(monkeypatch):
    reach = iter([False, True])  # unreachable before start, reachable after
    monkeypatch.setattr(setup, "_ollama_reachable", lambda *a, **k: next(reach, True))
    monkeypatch.setattr(setup, "_docker_available", lambda: True)
    monkeypatch.setattr(setup, "_container_exists", lambda name: False)
    calls = []

    def _run(cmd, **k):
        calls.append(cmd)
        return mock.Mock(returncode=0, stderr="")

    monkeypatch.setattr(setup.subprocess, "run", _run)
    ok, _ = setup._ensure_ollama_serving()
    assert ok
    assert any("run" in c and "apecx-ollama" in c for c in calls), calls


def _mock_urlopen(monkeypatch, body: bytes):
    def _urlopen(_req, timeout=None):
        cm = mock.MagicMock()
        cm.__enter__.return_value = io.BytesIO(body)
        cm.__exit__.return_value = False
        return cm

    monkeypatch.setattr(setup, "_ollama_url", lambda: "http://x:11434")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def test_pull_http_success(monkeypatch):
    _mock_urlopen(monkeypatch, b'{"status":"pulling"}\n{"status":"success"}\n')
    ok, msg = setup._pull_ollama_model_http("m:1")
    assert ok and "m:1" in msg


def test_pull_http_error_line_is_failure(monkeypatch):
    _mock_urlopen(monkeypatch, b'{"error":"model not found"}\n')
    ok, msg = setup._pull_ollama_model_http("nope:1")
    assert not ok and "not found" in msg


def test_pull_http_truncated_stream_without_success_is_failure(monkeypatch):
    # Stream ends without the terminal `status: success` → must fail loud, not false-succeed.
    _mock_urlopen(monkeypatch, b'{"status":"pulling"}\n{"status":"verifying"}\n')
    ok, msg = setup._pull_ollama_model_http("m:1")
    assert not ok and "without success" in msg


def test_step_llm_remote_endpoint_is_ok(monkeypatch):
    monkeypatch.setenv("APECX_LLM_BASE_URL", "http://gpu.internal:8000/v1")
    r = setup._step_llm(interactive=False)
    assert r.status == "ok" and "remote" in r.detail.lower()


def test_step_llm_pulls_when_model_absent(monkeypatch):
    monkeypatch.setattr(setup, "_ensure_ollama_serving", lambda **k: (True, "started"))
    monkeypatch.setattr(setup, "_ollama_installed_models", lambda *a, **k: set())
    pulled = {}

    def _pull(model, **k):
        pulled["m"] = model
        return True, f"pulled {model}"

    monkeypatch.setattr(setup, "_pull_ollama_model_http", _pull)
    r = setup._step_llm(interactive=False)
    assert r.status == "ok"
    assert pulled["m"] == setup._ollama_model()


def test_step_llm_ok_when_model_present(monkeypatch):
    monkeypatch.setattr(setup, "_ensure_ollama_serving", lambda **k: (True, "adopted"))
    monkeypatch.setattr(setup, "_ollama_installed_models", lambda *a, **k: {setup._ollama_model()})
    r = setup._step_llm(interactive=False)
    assert r.status == "ok" and "already pulled" in r.detail


def test_probe_llm_unreachable_local_is_actionable(monkeypatch):
    monkeypatch.setenv("APECX_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(setup, "_ollama_reachable", lambda *a, **k: False)
    ok, detail = setup._probe_llm()
    assert not ok and "no local Ollama" in detail
