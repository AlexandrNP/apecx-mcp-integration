"""T13 step 3 — ``ScanViolation`` is enriched with "closest matches in
component library" suggestions when the composer rejects novel Python.

Contract (implementation_plan.md §T13 step 3):

    Composer integration: any novel Python is scanned before any run;
    unknown imports → reject with "Package X not available. Closest
    matches in component library: [...]" (use T03 RAG to suggest).

These tests verify the suggestion block lands in the
``ScanViolation.suggestions`` tuple + the exception message, using the
linear-scan catalog (no Ollama / no mpnet required). The RAG backend
uses the same ``_retrieve`` hook, so covering linear-scan is
sufficient to cover the wire.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from apecx_integration.composition.composer import Composer
from apecx_integration.composition.sandbox import ScanViolation

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"


class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    def __init__(self, canned: str):
        self.canned = canned

    def invoke(self, messages):
        return _PlaceholderResponse(self.canned)


def _make_factory(canned: str):
    def _factory(**_kwargs):
        return _PlaceholderLLM(canned)

    return _factory


# Novel Python that (1) uses a non-whitelisted import and (2) carries
# bio-workflow intent in its comments and identifiers. Retrieval over
# the source text should lift relevant catalog components.
BIO_INTENT_NOVEL_PYTHON_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: shell_workflow
    description: "Try to do entity extraction via subprocess."
    version: "0.1.0"
    steps:
      rogue_extractor:
        class: "generated.RogueExtractor"
        config: {}
    links: {}
    ```

    ```novel_python
    rogue_extractor: |
      import subprocess  # non-whitelisted

      class RogueExtractor:
          \"\"\"Extract pathogen entity names from a biomedical query.\"\"\"
          async def process(self, input_data, **kwargs):
              # Shell out to extract pathogen / virus / gene entities
              # from the user's free-text query.
              out = subprocess.run(
                  ["entity_extract", input_data["query"]],
                  check=True,
                  capture_output=True,
              )
              return {"entities": out.stdout.decode()}
    ```
    """
)


def test_scan_violation_carries_component_suggestions():
    composer = Composer.from_config(DEFAULT_CONFIG)
    # Force MONOLITHIC mode: this canned response is the yaml+novel_python format, not a JSON
    # spec. The shipped default is spec mode (this test predates it, which is why it silently
    # broke). After the 2026-07-01 scan-hoist the import scan runs in compose() for BOTH modes;
    # this asserts the monolithic path still raises ScanViolation with suggestions.
    composer._config = composer._config.model_copy(update={"composer_mode": "monolithic"})  # noqa: SLF001
    composer._llm_factory = _make_factory(BIO_INTENT_NOVEL_PYTHON_RESPONSE)

    with pytest.raises(ScanViolation) as excinfo:
        asyncio.run(composer.compose("shell-based extractor"))

    violation = excinfo.value
    assert violation.suggestions, (
        "ScanViolation.suggestions is empty — T13 step 3 expected the "
        "composer to surface at least one 'closest match' entry. "
        "If the catalog is configured but retrieval returned nothing, "
        "the suggestion lookup is broken."
    )
    joined = "\n".join(violation.suggestions)
    assert "entity_extraction" in joined, (
        "expected 'entity_extraction' in suggestions because the "
        "novel source mentions 'entity names' and 'extract pathogen'. "
        f"Got:\n{joined}"
    )


def test_scan_violation_message_contains_closest_matches_header():
    composer = Composer.from_config(DEFAULT_CONFIG)
    # Force MONOLITHIC mode: this canned response is the yaml+novel_python format, not a JSON
    # spec. The shipped default is spec mode (this test predates it, which is why it silently
    # broke). After the 2026-07-01 scan-hoist the import scan runs in compose() for BOTH modes;
    # this asserts the monolithic path still raises ScanViolation with suggestions.
    composer._config = composer._config.model_copy(update={"composer_mode": "monolithic"})  # noqa: SLF001
    composer._llm_factory = _make_factory(BIO_INTENT_NOVEL_PYTHON_RESPONSE)

    with pytest.raises(ScanViolation, match="Closest matches in component library"):
        asyncio.run(composer.compose("shell-based extractor"))


def test_standalone_scan_violation_has_no_suggestions_by_default():
    """Smoke: ScanViolation raised directly by the scanner (no composer
    in the loop) still has an empty ``suggestions`` tuple — the
    enrichment is a composer concern, not a scanner concern."""
    from apecx_integration.composition.sandbox import ScanResult

    result = ScanResult(violations=["Unknown import 'x' at line 1"])
    violation = ScanViolation(result)
    assert violation.suggestions == ()
    # Header is absent when suggestions are empty.
    assert "Closest matches" not in str(violation)
