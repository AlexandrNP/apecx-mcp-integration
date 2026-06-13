"""E3-6 live preflight — gated on a real Ollama daemon (no mocks).

Proves the loud-preflight contract against the real endpoint:

* a model that IS pulled (nemotron-3-nano:4b) passes preflight;
* a deliberately-bogus model name on the SAME reachable endpoint fails loud
  with the exact ``ollama pull <model>`` guidance.

Auto-skips when the daemon is unreachable, so an offline session stays green.
"""

from __future__ import annotations

import os

import pytest
import requests

from apecx_integration.agents import _llm_config
from apecx_integration.agents._llm_config import preflight_llm_model, resolve_llm_base_url

pytestmark = pytest.mark.integration

_REAL_MODEL = "nemotron-3-nano:4b"


def _daemon_reachable() -> bool:
    base = resolve_llm_base_url().rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base
    try:
        return requests.get(root + "/api/tags", timeout=3).ok
    except requests.RequestException:
        return False


def _model_pulled(model: str) -> bool:
    reachable, pulled = _llm_config._probe_model(model, resolve_llm_base_url())
    return reachable and pulled


needs_ollama = pytest.mark.skipif(
    not _daemon_reachable(), reason="needs a reachable Ollama daemon (APECX_LLM_BASE_URL)"
)


@pytest.fixture(autouse=True)
def _clear_preflight_cache():
    _llm_config._preflight_done.clear()
    yield
    _llm_config._preflight_done.clear()


@needs_ollama
@pytest.mark.skipif(
    not _model_pulled(_REAL_MODEL),
    reason=f"{_REAL_MODEL} not pulled (`ollama pull {_REAL_MODEL}`)",
)
def test_pulled_model_passes_preflight_live():
    # Does not raise — the model is really pulled on the live daemon.
    preflight_llm_model(model=_REAL_MODEL)


@needs_ollama
def test_bogus_model_fails_loud_live():
    bogus = "definitely-not-a-real-model:0b"
    with pytest.raises(RuntimeError) as exc:
        preflight_llm_model(model=bogus)
    assert f"ollama pull {bogus}" in str(exc.value)


def test_unreachable_endpoint_warns_does_not_raise():
    # A port nothing listens on — reachability fails, preflight must not crash.
    bogus_url = os.environ.get("APECX_PREFLIGHT_TEST_DOWN_URL", "http://127.0.0.1:1/v1")
    preflight_llm_model(model=_REAL_MODEL, base_url=bogus_url)
