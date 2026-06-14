"""Pin the apecx-setup composer-model coverage (R2 / E3-6, 2026-06-13).

Before R2, ``apecx-setup llm`` pulled only the single synthesis model
(``resolve_llm_model()`` = ``nemotron-3-nano:4b``). The COMPOSER is a
separate tier with its OWN per-role models in ``composer_config.yml``
(top-level ``llm_model`` default + ``model_roles`` drafter/planner/
reviewer). A fresh install that then ran the composer 404'd on the first
composer call because those models were never pulled.

R2 adds an opt-in (``--with-composer``) that unions the composer's
EFFECTIVE model set (env-overridden, mirroring the composer's own
``_apply_llm_env_overrides``) into the set ``apecx-setup llm`` ensures.
The synthesis model is ALWAYS ensured; the composer models are the
gated addition.

Contracts pinned:
  * ``_composer_models`` reads composer_config.yml + returns the
    DISTINCT non-empty set (default + per-role) — against the REAL
    shipped config.
  * env overrides (``APECX_LLM_MODEL`` / ``APECX_LLM_MODEL_<ROLE>``)
    flow into the resolved set (parity with the composer).
  * ``_models_to_ensure`` always includes the synthesis model; adds
    composer models only under ``with_composer=True``; de-dups.
  * idempotency: an already-pulled model is reported as ensured, not
    re-pulled (no ``ollama pull`` subprocess for present models).
  * Ollama-down / CLI-missing → graceful ``skipped`` with a NON-EMPTY
    detail that names the model set (CC-1), no raise.
  * a failed pull → ``partial`` (not ``fail``, not raise); the models
    that DID land are still reported.

The Ollama wire (``/api/tags`` + ``ollama pull``) is MOCKED here — we
never trigger a real 14 GB download in the suite. The live-Ollama parity
check lives in ``test_setup_composer_models_live`` below (auto-skips
when the daemon is unreachable).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from unittest import mock

import pytest

from apecx_integration.cli import setup

# --------------------------------------------------------------------------
# Collector — against the REAL shipped composer_config.yml
# --------------------------------------------------------------------------


def test_composer_models_reads_real_config_distinct_nonempty():
    """The collector returns the composer's declared models (default +
    every per-role binding) as a distinct, non-empty list."""
    models = setup._composer_models()

    assert models, "composer model set must be non-empty against the real config"
    # The shipped config: default mistral-small + drafter mistral-nemo +
    # planner/reviewer nemotron (the latter two collapse to one entry).
    assert "mistral-small:latest" in models
    assert "mistral-nemo:latest" in models
    assert "nemotron-3-nano:4b" in models
    assert len(models) == len(set(models)), "models must be de-duplicated"


def test_composer_models_empty_when_config_absent(tmp_path):
    """Missing config file → empty list (graceful, no raise)."""
    assert setup._composer_models(tmp_path / "nope.yml") == []


def test_composer_models_honors_env_overrides(monkeypatch):
    """Operator env overrides flow into the resolved set, mirroring the
    composer's own ``_apply_llm_env_overrides``."""
    monkeypatch.setenv("APECX_LLM_MODEL", "llama3:70b")  # top-level default
    monkeypatch.setenv("APECX_LLM_MODEL_DRAFTER", "qwen2:7b")  # per-role

    models = setup._composer_models()

    assert "llama3:70b" in models
    assert "qwen2:7b" in models
    assert "mistral-small:latest" not in models  # overridden default
    assert "mistral-nemo:latest" not in models  # overridden drafter


# --------------------------------------------------------------------------
# Union set — synthesis always; composer gated
# --------------------------------------------------------------------------


def test_models_to_ensure_synthesis_only_by_default():
    """Without ``with_composer`` the ensured set is exactly the
    synthesis model — the default workflow path."""
    models = setup._models_to_ensure(with_composer=False)
    assert models == [setup._ollama_model()]


def test_models_to_ensure_unions_composer_and_dedups():
    """``with_composer=True`` unions the composer set; the synthesis
    model leads; duplicates (planner/reviewer == synthesis) collapse."""
    models = setup._models_to_ensure(with_composer=True)

    assert models[0] == setup._ollama_model()  # synthesis leads
    assert "mistral-small:latest" in models
    assert "mistral-nemo:latest" in models
    assert len(models) == len(set(models))
    # Synthesis-only is a subset of with-composer.
    assert set(setup._models_to_ensure(with_composer=False)) <= set(models)


# --------------------------------------------------------------------------
# _step_llm — mocked Ollama wire (NO real download)
# --------------------------------------------------------------------------


class _FakeTagsResponse:
    """Minimal context-manager stand-in for urllib's /api/tags response."""

    def __init__(self, model_names: list[str]):
        self._payload = json.dumps({"models": [{"name": n} for n in model_names]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _patch_ready_daemon():
    """Common patches: ollama on PATH + daemon reachable."""
    return (
        mock.patch.object(setup.shutil, "which", return_value="/usr/local/bin/ollama"),
        mock.patch.object(setup, "_offer_start_ollama_daemon", return_value=True),
    )


def test_step_llm_idempotent_skips_present_models():
    """All required models already pulled → status ok, every model
    reported ensured, ZERO ``ollama pull`` subprocesses."""
    wanted = setup._models_to_ensure(with_composer=True)
    which_p, daemon_p = _patch_ready_daemon()
    with (
        which_p,
        daemon_p,
        mock.patch.object(urllib.request, "urlopen", return_value=_FakeTagsResponse(wanted)),
        mock.patch.object(setup.subprocess, "run") as run_mock,
    ):
        result = setup._step_llm(interactive=False, with_composer=True)

    assert result.status == "ok"
    for model in wanted:
        assert model in result.detail
    run_mock.assert_not_called()  # nothing pulled


def test_step_llm_pulls_only_missing_models():
    """Only the absent composer model is pulled; present ones skipped."""
    wanted = setup._models_to_ensure(with_composer=True)
    present = [wanted[0]]  # only synthesis present
    missing = wanted[1:]
    which_p, daemon_p = _patch_ready_daemon()
    with (
        which_p,
        daemon_p,
        mock.patch.object(urllib.request, "urlopen", return_value=_FakeTagsResponse(present)),
        mock.patch.object(
            setup.subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as run_mock,
    ):
        result = setup._step_llm(interactive=False, with_composer=True)

    assert result.status == "ok"
    pulled = [c.args[0][2] for c in run_mock.call_args_list]  # ["ollama","pull",MODEL]
    assert sorted(pulled) == sorted(missing)


def test_step_llm_failed_pull_is_partial_not_raise():
    """A non-zero ``ollama pull`` → status partial (chain continues),
    the models that landed still reported. Never raises."""
    wanted = setup._models_to_ensure(with_composer=True)
    which_p, daemon_p = _patch_ready_daemon()
    with (
        which_p,
        daemon_p,
        mock.patch.object(urllib.request, "urlopen", return_value=_FakeTagsResponse([])),
        mock.patch.object(setup.subprocess, "run", return_value=mock.Mock(returncode=1)),
    ):
        result = setup._step_llm(interactive=False, with_composer=True)

    assert result.status == "partial"
    assert "FAILED" in result.detail
    for model in wanted:
        assert model in result.detail


def test_step_llm_ollama_down_skips_with_nonempty_model_detail():
    """Daemon unreachable → skipped, NON-EMPTY detail naming the model
    set it would have ensured (CC-1). No raise."""
    with (
        mock.patch.object(setup.shutil, "which", return_value="/usr/local/bin/ollama"),
        mock.patch.object(setup, "_offer_start_ollama_daemon", return_value=False),
    ):
        result = setup._step_llm(interactive=False, with_composer=True)

    assert result.status == "skipped"
    assert result.detail
    for model in setup._models_to_ensure(with_composer=True):
        assert model in result.detail


# --------------------------------------------------------------------------
# Live-Ollama parity (cheap, read-only; auto-skips when unreachable)
# --------------------------------------------------------------------------


def _ollama_tags_or_skip() -> set[str]:
    try:
        with urllib.request.urlopen(setup._ollama_url() + "/api/tags", timeout=2) as resp:
            return {m.get("name") for m in json.loads(resp.read()).get("models") or []}
    except (urllib.error.URLError, OSError):
        pytest.skip("Ollama daemon unreachable — live parity check skipped")


def test_collector_set_matches_live_ollama_tags():
    """Against the live daemon, the with-composer set includes the
    synthesis model + the composer models, and we can partition them
    into already-present vs would-be-pulled from real /api/tags. No
    download triggered (read-only)."""
    installed = _ollama_tags_or_skip()

    wanted = setup._models_to_ensure(with_composer=True)
    assert setup._ollama_model() in wanted
    assert "mistral-small:latest" in wanted
    assert "mistral-nemo:latest" in wanted

    present = [m for m in wanted if m in installed]
    pull = [m for m in wanted if m not in installed]
    # The partition must cover the full set (no model lost).
    assert sorted(present + pull) == sorted(wanted)
