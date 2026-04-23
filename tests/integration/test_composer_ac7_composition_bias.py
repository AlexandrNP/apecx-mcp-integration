"""T-COMP Phase 5 / AC7: composition-bias regression test.

Contract (composer_task_spec.md line 177):

    Given a fixture prompt that is fully covered by library components,
    the composer emits zero ``novel_python`` entries. If this fails,
    the library prompt isn't biasing composition hard enough.

The fixture prompt below names three capabilities that the
``workflows/violin_bvbrc/manifest.yml`` catalog covers directly:

    - ``entity_extraction``      (step_id "1")
    - ``synonym_cache_lookup``   (step_id "3a")
    - ``bvbrc_snapshot_match``   (step_id "2")

If the LLM still emits novel Python, the ``composition_bias.md``
prompt file is not steering the model hard enough — that is what
this test exists to catch.

**Operator-run only.** Auto-skips per the same rules as the other
live-Ollama tests (`APECX_SKIP_LIVE_LLM=1` / daemon unreachable /
model not pulled).

Honest caveat — this is an LLM regression test, so it is
*fundamentally* probabilistic. At ``temperature=0`` Mistral is
close to deterministic but not guaranteed across model version
bumps (R2 in the task spec). When this test flaps, the model bump
is the first suspect, not the composer code. Pin the model name in
the env vars and rerun before filing a composer bug.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

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


def _composer_runtime_deps_importable() -> bool:
    try:
        import apecx_db_integration  # noqa: F401
    except ImportError:
        return False
    return True


SKIP_LIVE_LLM_OPTOUT = (
    "APECX_SKIP_LIVE_LLM=1 is set — live-LLM tests explicitly skipped."
)
SKIP_OLLAMA_UNREACHABLE = (
    f"Ollama daemon unreachable at {OLLAMA_URL} or model {OLLAMA_MODEL} "
    f"not pulled. Run `ollama serve` + `ollama pull {OLLAMA_MODEL}`."
)
SKIP_DEPS_MISSING = (
    "apecx_db_integration not importable — install via "
    "`pip install -e ../apecx-db-integration` before running live-LLM "
    "composer tests."
)


AC7_FIXTURE_PROMPT = (
    "Build a workflow that extracts pathogen and gene entity names "
    "from a user's free-text biomedical query via the "
    "entity_extraction component, then checks each extracted term "
    "against the synonym_cache_lookup component to skip terms a "
    "human reviewer has already blessed, and finally maps the "
    "survivors to BV-BRC genome and protein ids via the "
    "bvbrc_snapshot_match component. The workflow links the three "
    "steps with plain DirectLinks."
)


@pytest.mark.skipif(_skip_live_llm_requested(), reason=SKIP_LIVE_LLM_OPTOUT)
@pytest.mark.skipif(
    not _ollama_reachable_with_model(OLLAMA_MODEL),
    reason=SKIP_OLLAMA_UNREACHABLE,
)
@pytest.mark.skipif(
    not _composer_runtime_deps_importable(), reason=SKIP_DEPS_MISSING
)
def test_composer_prefers_library_when_prompt_is_fully_covered(monkeypatch):
    monkeypatch.setenv("APECX_LLM_BASE_URL", f"{OLLAMA_URL}/v1")
    monkeypatch.setenv("APECX_LLM_MODEL", OLLAMA_MODEL)
    monkeypatch.setenv("APECX_LLM_TEMPERATURE", "0.0")
    monkeypatch.setenv("APECX_LLM_MAX_TOKENS", "2048")

    composer = Composer.from_config(DEFAULT_CONFIG)
    result = asyncio.run(composer.compose(AC7_FIXTURE_PROMPT))

    assert result.novel_python == {}, (
        "AC7 violation: the LLM emitted novel Python even though the "
        "fixture prompt is fully covered by library components. "
        "Investigate composition_bias.md prompt tuning or the retrieved "
        "component list. "
        f"Novel-step ids: {sorted(result.novel_python.keys())}"
    )

    # Retrieval must have surfaced at least one of the three named
    # components — if not, the catalog lookup itself regressed, which
    # would cause the LLM to fall back to novel Python for legitimate
    # reasons. Keep this assertion loose: we care that retrieval is
    # functioning, not which exact set of hits came back.
    expected_any = {
        "entity_extraction",
        "synonym_cache_lookup",
        "bvbrc_snapshot_match",
    }
    retrieved_names = {
        name.split("/")[-1].split(":")[0]
        for name in result.retrieved_components
    }
    assert retrieved_names & expected_any, (
        "retrieval returned nothing from the three named components — "
        "catalog lookup appears broken. "
        f"Retrieved: {sorted(result.retrieved_components)}"
    )
