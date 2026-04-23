"""T-COMP Phase 2 live-Ollama integration test (operator-run).

Per the workspace mocks-policy parity rule, the placeholder-LLM
tests in ``test_composer_phase2.py`` need a matching integration
test that exercises the same compose() pipeline against a real
Ollama backend. This file is that pair.

**Operator-run only.** Claude does not invoke this test.
Auto-skips when:
- ``APECX_SKIP_LIVE_LLM=1`` is set (Claude's opt-out — see
  session_friction_log #1), OR
- Ollama daemon is unreachable, OR
- the target model is not pulled.

Scope: one happy-path assertion. The fixture prompt is "Build a
workflow that extracts biomedical entities from a user query and
ranks the results." The assertion: compose() returns a non-empty
ComposedWorkflow whose yaml loads via yaml.safe_load without
raising. We DON'T assert that the resulting workflow is
well-composed (that's a prompt-quality measurement, Phase 5 scope);
only that the pipeline runs end-to-end.

Wall-time budget: 30–120s on mistral-nemo:latest. The LLM-call
token budget is inherited from ComposerConfig.max_tokens (default
4096); operators who want tighter bounds set APECX_LLM_MAX_TOKENS.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
import yaml

from apecx_integration.composition.composer import Composer

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral-nemo:latest")


def _skip_live_llm_requested() -> bool:
    return os.environ.get("APECX_SKIP_LIVE_LLM") == "1"


def _ollama_reachable_with_model(model: str) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return model in names
    except Exception:
        return False


SKIP_LIVE_LLM_OPTOUT = (
    "APECX_SKIP_LIVE_LLM=1 is set — live-LLM tests explicitly skipped."
)
SKIP_OLLAMA_UNREACHABLE = (
    f"Ollama daemon unreachable at {OLLAMA_URL} or model {OLLAMA_MODEL} "
    f"not pulled. Run `ollama serve` + `ollama pull {OLLAMA_MODEL}`."
)


@pytest.mark.skipif(_skip_live_llm_requested(), reason=SKIP_LIVE_LLM_OPTOUT)
@pytest.mark.skipif(
    not _ollama_reachable_with_model(OLLAMA_MODEL),
    reason=SKIP_OLLAMA_UNREACHABLE,
)
def test_compose_against_ollama_produces_loadable_yaml(monkeypatch):
    """One happy-path assertion per spec §6 P2 exit criterion.

    Operator: set ``APECX_LLM_BASE_URL=http://localhost:11434/v1`` +
    ``APECX_LLM_MODEL=mistral-nemo:latest`` before running (the
    composer config file already defaults these, but explicit env
    vars win).
    """
    monkeypatch.setenv("APECX_LLM_BASE_URL", f"{OLLAMA_URL}/v1")
    monkeypatch.setenv("APECX_LLM_MODEL", OLLAMA_MODEL)
    # Tight bounds so the test doesn't hang for minutes on a bad model.
    monkeypatch.setenv("APECX_LLM_TEMPERATURE", "0.0")
    monkeypatch.setenv("APECX_LLM_MAX_TOKENS", "1024")

    composer = Composer.from_config(DEFAULT_CONFIG)

    fixture_prompt = (
        "Build a workflow that extracts biomedical entities from a user "
        "query using the entity_extraction component, and ranks the "
        "results via the result_ranking component."
    )
    result = asyncio.run(composer.compose(fixture_prompt))

    # The yaml must at least parse as a dict.
    workflow = yaml.safe_load(result.yaml_bytes.decode("utf-8"))
    assert isinstance(workflow, dict), (
        f"expected a mapping; got {type(workflow).__name__}"
    )

    # Retrieved_components audit trail is non-empty (the catalog had
    # 9 components; the fixture prompt contains terms that match
    # several of them).
    assert result.retrieved_components, (
        "expected non-empty retrieved_components from catalog search"
    )

    # The LLM model hash is deterministic given the model name — pin it
    # for regression detection.
    assert result.llm_model == OLLAMA_MODEL
    assert len(result.llm_model_version_hash) == 64  # sha256 hex


@pytest.mark.skipif(_skip_live_llm_requested(), reason=SKIP_LIVE_LLM_OPTOUT)
@pytest.mark.skipif(
    not _ollama_reachable_with_model(OLLAMA_MODEL),
    reason=SKIP_OLLAMA_UNREACHABLE,
)
def test_compose_against_ollama_with_empty_catalog(monkeypatch, tmp_path):
    """When the catalog is empty (no library to compose from), the LLM
    should still produce SOMETHING — either novel Python or a yaml that
    notes the capability gap. Either way, the compose() call must not
    raise.

    This catches the failure mode "composer hangs / errors when catalog
    is empty" which would break the Phase-2 pipeline for any operator
    who forgot to configure catalog paths.
    """
    monkeypatch.setenv("APECX_LLM_BASE_URL", f"{OLLAMA_URL}/v1")
    monkeypatch.setenv("APECX_LLM_MODEL", OLLAMA_MODEL)
    monkeypatch.setenv("APECX_LLM_TEMPERATURE", "0.0")
    monkeypatch.setenv("APECX_LLM_MAX_TOKENS", "1024")

    # Build a minimal composer config with NO catalog paths. Reuse the
    # shipped prompts + whitelist so we don't diverge from the real
    # Phase-2 contract.
    cfg = tmp_path / "empty_catalog_composer.yml"
    default_dir = DEFAULT_CONFIG.parent
    cfg.write_text(
        f"library_version: '0.1.0-empty'\n"
        f"prompt_dir: '{default_dir / 'composer_prompts'}'\n"
        f"component_catalog_paths: []\n"
        f"sandbox_whitelist_path: null\n"
        f"max_tokens: 1024\n"
        f"temperature: 0.0\n"
    )
    composer = Composer.from_config(cfg)
    assert len(composer.catalog) == 0

    # Compose — this may raise ComposerResponseError if the LLM's output
    # is unparseable, but it must NOT hang or crash on the empty catalog.
    # We don't assert on the shape of the result; this test is about the
    # pipeline's robustness.
    try:
        result = asyncio.run(composer.compose(
            "Write a workflow that does X (there is no library; improvise)."
        ))
        assert result.retrieved_components == ()
    except Exception as exc:
        # ComposerResponseError is acceptable here — the LLM is being asked
        # to improvise and may return unparseable output. Not a failure
        # of our pipeline.
        from apecx_integration.composition.composer import ComposerResponseError
        assert isinstance(exc, ComposerResponseError), (
            f"expected ComposerResponseError on bad LLM output; got "
            f"{type(exc).__name__}: {exc}"
        )
