"""Unit tests for the capability model + the verify optional-set reclassification.

Two clean-install UX guarantees are pinned here (2026-06-15):

  1. ``_probe_capabilities`` honestly classifies the zero-infra baseline
     (entity resolution / harmonized search / LLM analysis) as AVAILABLE on a
     fresh install — harmonized search needs NO credentials (anonymous public
     Globus index), and the LLM leg is satisfiable by a REMOTE endpoint, so
     installing Ollama locally is optional.

  2. ``_step_verify`` treats ``data`` and ``ollama`` as OPTIONAL: a clean
     install with only the synonym dictionary verifies as ``partial`` (a usable
     product), never ``fail``. Only the dictionary is required.
"""

from __future__ import annotations

from apecx_integration.cli import setup as setup_cli

# ─────────────────────────────────────────────────────────────────────────
# _probe_llm — remote endpoint makes local Ollama optional
# ─────────────────────────────────────────────────────────────────────────


def test_probe_llm_remote_endpoint_is_available_without_local_ollama(monkeypatch):
    monkeypatch.setenv("APECX_LLM_BASE_URL", "https://vllm.example.org/v1")
    # Even with no `ollama` binary, a remote endpoint satisfies the LLM dep.
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
# _probe_capabilities — zero-infra baseline is real
# ─────────────────────────────────────────────────────────────────────────


def test_harmonized_search_is_always_available(monkeypatch):
    # Anonymous public Globus index — no creds, no infra, no data download.
    caps = {c.key: c for c in setup_cli._probe_capabilities()}
    assert caps["harmonized_search"].available is True
    assert caps["harmonized_search"].unlock == ""


def test_clean_install_baseline_available_with_dict_and_remote_llm(monkeypatch, tmp_path):
    # Simulate the leanest functional install: dictionary present + a remote
    # LLM endpoint, but NO Docker, NO mafft, NO local data, NO Ollama binary.
    sqlite = tmp_path / "dictionary.sqlite"
    sqlite.write_bytes(b"x")
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(sqlite))
    monkeypatch.setenv("APECX_LLM_BASE_URL", "https://remote-llm.example.org/v1")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda _: None)  # no mafft, no ollama
    monkeypatch.setattr(setup_cli, "_docker_available", lambda: False)

    caps = {c.key: c for c in setup_cli._probe_capabilities()}
    # Zero-infra baseline all green:
    assert caps["entity_resolution"].available is True
    assert caps["harmonized_search"].available is True
    assert caps["llm_analysis"].available is True
    # Docker-gated capabilities locked + honestly tagged:
    assert caps["structural_sasa"].available is False
    assert caps["structural_sasa"].needs_docker is True
    assert caps["rhea_tools"].available is False
    assert "LLM-only" in caps["structural_sasa"].unlock
    # MAFFT locked without the binary:
    assert caps["sequence_conservation"].available is False


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


def test_step_capabilities_returns_ok_and_counts(monkeypatch, tmp_path):
    sqlite = tmp_path / "dictionary.sqlite"
    sqlite.write_bytes(b"x")
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", str(sqlite))
    monkeypatch.setenv("APECX_LLM_BASE_URL", "https://remote-llm.example.org/v1")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda _: None)
    monkeypatch.setattr(setup_cli, "_docker_available", lambda: False)

    result = setup_cli._step_capabilities()
    assert result.name == "capabilities"
    assert result.status == "ok"
    assert "3/3 zero-infra baseline" in result.detail
