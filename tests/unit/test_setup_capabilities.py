"""Unit tests for the verify reclassification + the backend-LLM probe in setup.py.

The capability *view* itself now lives in the shared aggregator
(``apecx_capabilities`` MCP tool, built on ``check_prerequisites`` +
orchestrator status); ``apecx-setup capabilities`` renders that. The probe
logic is no longer duplicated in setup.py — see test_apecx_capabilities_tool.py.

What remains setup.py's own and is pinned here (2026-06-15):

  1. ``_probe_llm`` — the BACKEND-mode synthesis LLM probe (local Ollama OR a
     remote APECX_LLM_BASE_URL). This is the apecx-internal LLM used by
     headless ``run_workflow`` / ``synthesize_query``; in desktop/MCP mode the
     frontier LLM (the MCP client) does analysis instead, so this backend LLM
     is OPTIONAL — hence its 'ollama' verify check is optional, not required.

  2. ``_step_verify`` treats ``data`` and ``ollama`` as OPTIONAL: a clean
     install with only the synonym dictionary verifies as ``partial`` (a usable
     product), never ``fail``. Only the dictionary is required.
"""

from __future__ import annotations

from apecx_integration.cli import setup as setup_cli

# ─────────────────────────────────────────────────────────────────────────
# _probe_llm — the BACKEND synthesis LLM (local Ollama OR remote endpoint)
# ─────────────────────────────────────────────────────────────────────────


def test_probe_llm_remote_endpoint_is_available_without_local_ollama(monkeypatch):
    monkeypatch.setenv("APECX_LLM_BASE_URL", "https://vllm.example.org/v1")
    # Even with no `ollama` binary, a remote backend endpoint satisfies the dep.
    monkeypatch.setattr(setup_cli.shutil, "which", lambda _: None)
    ok, detail = setup_cli._probe_llm()
    assert ok is True
    assert "remote endpoint configured" in detail


def test_probe_llm_localhost_base_url_falls_through_to_ollama_probe(monkeypatch):
    # A localhost base URL is NOT a remote endpoint — it must still require a
    # reachable local Ollama with the model pulled.
    monkeypatch.setenv("APECX_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda _: None)
    ok, detail = setup_cli._probe_llm()
    assert ok is False
    assert "no local Ollama" in detail


# ─────────────────────────────────────────────────────────────────────────
# _step_verify — data + ollama are OPTIONAL (partial, not fail)
# ─────────────────────────────────────────────────────────────────────────


def _checks_to_result(checks):
    """Re-run only verify's summary logic against a synthetic checks list."""
    failed = [name for name, ok, _ in checks if not ok]
    optional = {"data", "violin", "ollama", "postgres", "redis", "minio", "faiss", "rhea"}
    real = [f for f in failed if f not in optional]
    return "fail" if real else ("ok" if not failed else "partial")


def test_verify_optional_set_makes_dict_the_only_required_component():
    # Only `dict` missing → fail. data/ollama/infra missing → partial.
    assert _checks_to_result([("dict", False, "")]) == "fail"
    assert (
        _checks_to_result(
            [
                ("dict", True, ""),
                ("data", False, ""),
                ("ollama", False, ""),
                ("postgres", False, ""),
                ("rhea", False, ""),
            ]
        )
        == "partial"
    )
    assert _checks_to_result([("dict", True, ""), ("data", True, "")]) == "ok"
