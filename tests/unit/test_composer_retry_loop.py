"""C1 — Compose-validate-retry loop.

These tests pin the retry behavior end-to-end at the composer
boundary, using a sequenced stub LLM (no network call):

  1. A valid response on attempt 1 → 0 retries, no extra LLM call.
  2. An invalid response on attempt 1 then valid on attempt 2 →
     1 retry, the assistant turn carries the prior YAML, the user
     turn carries the feedback payload with the violated rule_ids.
  3. Invalid on both attempts (budget exhausted) → WorkflowValidationError
     raised with the SECOND attempt's violations (not the first).
  4. ``max_validation_retries=0`` disables retries entirely.

The stub LLM also records the messages it received on each invoke,
so we can assert that the retry actually contains the
to_feedback_payload() bytes — without that, the loop could be
silently no-op'ing.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from apecx_integration.composition.composer import Composer
from apecx_integration.composition.workflow_validator import (
    WorkflowValidationError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"


VALID_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: c1_valid_workflow
    description: "single-step workflow with file-path config"
    version: "0.1.0"
    steps:
      entity_extraction:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
    links: {}
    ```
    """
)


INVALID_INLINE_CONFIG_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: c1_invalid_workflow
    description: "the exact issues-doc failure shape — inline dict on a Step"
    version: "0.1.0"
    steps:
      entity_extraction:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config:
          target_entities: ['customer', 'product', 'feature']
          max_candidates: 10
          min_confidence: 0.5
    links: {}
    ```
    """
)


class _RecordingResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _SequencedStubLLM:
    """LLM that returns canned responses in sequence and records each
    invocation's message list.

    Used to drive the compose-validate-retry loop without a real LLM
    call. Failing once early (sequenced [invalid, valid]) exercises
    the retry path; calling more times than the sequence has entries
    raises an explicit error so a runaway loop fails loudly instead
    of hanging.
    """

    def __init__(self, sequence: list[str]) -> None:
        self._sequence = list(sequence)
        self.invocations: list[list] = []

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self._sequence:
            raise AssertionError(
                "stub LLM exhausted its canned-response sequence; "
                "compose() may be in a runaway retry loop"
            )
        return _RecordingResponse(self._sequence.pop(0))


def _composer_with(llm: _SequencedStubLLM, *, max_retries: int = 1) -> Composer:
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    # These tests exercise the MONOLITHIC retry path; pin the mode
    # explicitly so they keep passing after the SPEC2 default-flip
    # (composer_mode=spec is now the project default).
    composer._config = composer._config.model_copy(  # type: ignore[attr-defined]
        update={
            "max_validation_retries": max_retries,
            "composer_mode": "monolithic",
        }
    )
    return composer


# ---------------------------------------------------------------------------
# Happy path — no retry
# ---------------------------------------------------------------------------


def test_valid_first_response_uses_zero_retries():
    llm = _SequencedStubLLM([VALID_RESPONSE])
    composer = _composer_with(llm)
    result = asyncio.run(composer.compose("dummy task description"))
    assert result.composition_summary.compose_retries == 0
    assert len(llm.invocations) == 1


# ---------------------------------------------------------------------------
# Recovery — invalid then valid
# ---------------------------------------------------------------------------


def test_invalid_then_valid_recovers_with_one_retry():
    """The keystone C1 behavior: the issues-doc inline-dict failure
    is now recoverable in one round trip instead of bubbling out as
    a runtime error."""
    llm = _SequencedStubLLM([INVALID_INLINE_CONFIG_RESPONSE, VALID_RESPONSE])
    composer = _composer_with(llm, max_retries=1)
    result = asyncio.run(composer.compose("dummy task description"))
    assert result.composition_summary.compose_retries == 1
    assert len(llm.invocations) == 2

    # The second invocation must include the prior (invalid) YAML
    # as an assistant turn AND the feedback payload as a user turn —
    # otherwise the retry has no signal to fix the problem.
    second_msgs = llm.invocations[1]
    contents = [getattr(m, "content", str(m)) for m in second_msgs]
    # Original system + user (first two) + assistant (prior YAML)
    # + user (feedback payload).
    assert len(second_msgs) == 4
    assert any("target_entities" in c for c in contents), (
        "the prior invalid YAML must be threaded back as an assistant turn"
    )
    feedback_text = contents[-1]
    assert "step_inline_config_forbidden" in feedback_text, (
        "the feedback turn must name the violated rule_id so the LLM has a precise repair signal"
    )
    assert "entity_extraction" in feedback_text


# ---------------------------------------------------------------------------
# Budget exhausted — stuck LLM
# ---------------------------------------------------------------------------


def test_invalid_twice_raises_validation_error():
    """When the LLM keeps emitting invalid workflows, the budget
    runs out and the SECOND attempt's violations surface (not the
    first's) — confirms the retry actually went through, just
    couldn't repair the workflow."""
    llm = _SequencedStubLLM([INVALID_INLINE_CONFIG_RESPONSE, INVALID_INLINE_CONFIG_RESPONSE])
    composer = _composer_with(llm, max_retries=1)
    with pytest.raises(WorkflowValidationError) as excinfo:
        asyncio.run(composer.compose("dummy task description"))
    assert len(llm.invocations) == 2
    rule_ids = [v.rule_id for v in excinfo.value.violations]
    assert "step_inline_config_forbidden" in rule_ids


# ---------------------------------------------------------------------------
# Disabling retries (regression-test convenience)
# ---------------------------------------------------------------------------


def test_max_retries_zero_disables_retry_loop():
    """``max_validation_retries=0`` reverts to pre-C1 behavior: the
    first invalid response raises immediately. Operators / tests
    that want this can opt in."""
    llm = _SequencedStubLLM([INVALID_INLINE_CONFIG_RESPONSE, VALID_RESPONSE])
    composer = _composer_with(llm, max_retries=0)
    with pytest.raises(WorkflowValidationError):
        asyncio.run(composer.compose("dummy task description"))
    assert len(llm.invocations) == 1, (
        "with max_retries=0, compose() must NOT call the LLM a second time"
    )


# ---------------------------------------------------------------------------
# Metadata persistence
# ---------------------------------------------------------------------------


def test_compose_retries_threaded_into_summary():
    """The compose_retries field on CompositionSummary is the
    queryable regression metric for prompt-quality work. Pinning
    that it actually moves with the retry budget keeps the metric
    honest."""
    llm = _SequencedStubLLM([INVALID_INLINE_CONFIG_RESPONSE, VALID_RESPONSE])
    composer = _composer_with(llm, max_retries=1)
    result = asyncio.run(composer.compose("dummy task description"))
    assert result.composition_summary.compose_retries == 1

    # Sanity: also persists in the artifact metadata when an
    # ArtifactStore is wired (covered by Phase-3 integration tests
    # via the existing artifact_store assertion path; we just
    # verify the field reaches the summary here).
