"""C1 parse-error retry path.

Real ollama E2E run on 2026-05-11 surfaced a failure mode the
original C1 design didn't cover: the LLM emitted three yaml fenced
blocks, the first one a bare LIST (not a mapping). The composer's
parser raised ``ComposerResponseError`` with message "yaml block
must be a mapping at top level, got list" BEFORE the retry loop
got a chance to fire. The retry was designed to catch
WorkflowValidationError only.

The expanded retry surface (C1+) covers the parse-shape case too:
any ``ComposerResponseError`` whose message names a repairable
shape error triggers one round of feedback-prompted retry.

These tests pin:

  1. The retry fires when the parser raises "must be a mapping at
     top level".
  2. The retry does NOT fire on unrepairable parse errors (empty
     content, no yaml fence).
  3. The feedback payload mentions the actual error so the LLM can
     correct shape-wise.
  4. Budget caps still apply.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from apecx_integration.composition.composer import (
    Composer,
    ComposerResponseError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"


LIST_AT_TOP_LEVEL_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    - step_a
    - step_b
    ```
    """
)

VALID_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: c1_parse_retry_valid
    description: minimal valid workflow
    version: "0.1.0"
    steps:
      entity_extraction:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
    links: {}
    ```
    """
)

NO_YAML_FENCE_RESPONSE = "I refuse to emit a fenced block. Sorry."


class _RecordingResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _SequencedStubLLM:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = list(sequence)
        self.invocations: list[list] = []

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self._sequence:
            raise AssertionError("stub LLM exhausted its sequence; runaway retry loop?")
        return _RecordingResponse(self._sequence.pop(0))


def _composer_with(llm, *, max_retries: int = 1) -> Composer:
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    # Pin monolithic mode — these tests use YAML responses, which
    # the spec-mode parser (now default) would reject as JSON.
    composer._config = composer._config.model_copy(
        update={
            "max_validation_retries": max_retries,
            "composer_mode": "monolithic",
        }
    )
    return composer


def test_retry_fires_on_list_at_top_level_parse_error():
    """Issues-doc analogue: LLM emits a list-shape YAML; retry repairs."""
    llm = _SequencedStubLLM([LIST_AT_TOP_LEVEL_RESPONSE, VALID_RESPONSE])
    composer = _composer_with(llm, max_retries=1)
    result = asyncio.run(composer.compose("some prompt"))
    assert result.composition_summary.compose_retries == 1
    assert len(llm.invocations) == 2

    # The retry's feedback message must mention the actual error
    # so the LLM has a concrete shape-correction signal.
    second_msgs = llm.invocations[1]
    contents = [getattr(m, "content", str(m)) for m in second_msgs]
    feedback = contents[-1]
    assert "must be a mapping at top level" in feedback or "mapping" in feedback
    assert "name:" in feedback  # the expected-shape example


def test_no_retry_on_no_yaml_fence():
    """A ComposerResponseError that names "no ```yaml`` fence" is NOT
    a shape error the LLM can correct by emitting a different shape —
    the retry loop must NOT fire, the raw exception surfaces.
    """
    llm = _SequencedStubLLM([NO_YAML_FENCE_RESPONSE])
    composer = _composer_with(llm, max_retries=1)
    with pytest.raises(ComposerResponseError) as excinfo:
        asyncio.run(composer.compose("some prompt"))
    assert "yaml" in str(excinfo.value).lower()
    # Exactly one LLM invocation — no retry was attempted.
    assert len(llm.invocations) == 1


def test_parse_retry_respects_budget():
    """Two list-at-top-level responses with max_retries=1 → budget
    exhausted after attempt 2; raise the parse error."""
    llm = _SequencedStubLLM([LIST_AT_TOP_LEVEL_RESPONSE, LIST_AT_TOP_LEVEL_RESPONSE])
    composer = _composer_with(llm, max_retries=1)
    with pytest.raises(ComposerResponseError) as excinfo:
        asyncio.run(composer.compose("some prompt"))
    assert "mapping" in str(excinfo.value)
    assert len(llm.invocations) == 2


def test_parse_retry_disabled_when_max_retries_zero():
    llm = _SequencedStubLLM([LIST_AT_TOP_LEVEL_RESPONSE, VALID_RESPONSE])
    composer = _composer_with(llm, max_retries=0)
    with pytest.raises(ComposerResponseError):
        asyncio.run(composer.compose("some prompt"))
    assert len(llm.invocations) == 1
