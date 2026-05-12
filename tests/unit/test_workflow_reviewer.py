"""REVIEW-AGENT — workflow reviewer unit tests.

Pins the reviewer's contract:

  1. ``approved=true`` JSON verdict → ReviewVerdict(approved=True, ...).
  2. ``approved=false`` JSON verdict with concerns → ReviewVerdict
     with concerns surfaced.
  3. Malformed reviewer response → falls through with approved=True,
     review_used=False (the reviewer must NEVER permanently block
     a compose due to its own parse failures).
  4. LLM unreachable → same pass-through behavior.
  5. Composer's enable_review=True wires the reviewer into the
     pipeline and surfaces the verdict in CompositionSummary.

The reviewer prompt body is loaded from the shipped
``reviewer_system.md``; tests use a stub LLM so we don't depend on
Ollama for unit verification.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

from apecx_integration.composition.composer import Composer
from apecx_integration.composition.reviewer import (
    WorkflowReviewer,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
PROMPT_DIR = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_prompts"


# ---------------------------------------------------------------------------
# Stub LLM that returns a canned response
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLLM:
    def __init__(self, content: str | Exception) -> None:
        self._content = content
        self.invocations: list[list] = []

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if isinstance(self._content, Exception):
            raise self._content
        return _Resp(self._content)


def _factory(llm: _StubLLM):
    def _build(**_kw):
        return llm

    return _build


# ---------------------------------------------------------------------------
# Verdict shapes
# ---------------------------------------------------------------------------


APPROVED_JSON = textwrap.dedent(
    """\
    ```json
    {
      "approved": true,
      "reasoning": "Workflow uses RagSynthesisStep which matches the user's synthesis request.",
      "concerns": []
    }
    ```
    """
)

REJECTED_JSON = textwrap.dedent(
    """\
    ```json
    {
      "approved": false,
      "reasoning": "The user asked to extract entities but no EntityExtractionStep is present.",
      "concerns": [
        "Missing EntityExtractionStep — required for the NER task.",
        "SynthesisContextAssemblyStep is semantically unrelated."
      ]
    }
    ```
    """
)

MALFORMED_JSON = "I think this workflow looks fine. (no fenced JSON block)"


def test_reviewer_returns_approved_verdict():
    llm = _StubLLM(APPROVED_JSON)
    reviewer = WorkflowReviewer.from_prompt_dir(PROMPT_DIR, llm_factory=_factory(llm))
    assert reviewer is not None
    verdict = asyncio.run(
        reviewer.review(
            user_prompt="Synthesize an answer.",
            yaml_text="name: x\nsteps: {rag: {class: pkg.RagStep, config: x.yml}}\n",
        )
    )
    assert verdict.approved is True
    assert verdict.review_used is True
    assert "RagSynthesisStep" in verdict.reasoning


def test_reviewer_returns_rejected_with_concerns():
    llm = _StubLLM(REJECTED_JSON)
    reviewer = WorkflowReviewer.from_prompt_dir(PROMPT_DIR, llm_factory=_factory(llm))
    verdict = asyncio.run(
        reviewer.review(
            user_prompt="Extract entities from a query.",
            yaml_text="name: x\nsteps: {}\n",
        )
    )
    assert verdict.approved is False
    assert verdict.review_used is True
    assert len(verdict.concerns) == 2
    assert any("EntityExtractionStep" in c for c in verdict.concerns)


def test_reviewer_unparseable_response_passes_through():
    """The reviewer must NEVER permanently block a compose because
    its own response was unparseable. Pass through with
    approved=True + review_used=False so operators see the issue
    without losing the compose run."""
    llm = _StubLLM(MALFORMED_JSON)
    reviewer = WorkflowReviewer.from_prompt_dir(PROMPT_DIR, llm_factory=_factory(llm))
    verdict = asyncio.run(
        reviewer.review(
            user_prompt="any task",
            yaml_text="name: x\n",
        )
    )
    assert verdict.approved is True
    assert verdict.review_used is False


def test_reviewer_llm_exception_passes_through():
    """Same pass-through rule when the LLM itself raises (network
    failure, provider 5xx, etc.)."""
    llm = _StubLLM(ConnectionError("ollama unreachable"))
    reviewer = WorkflowReviewer.from_prompt_dir(PROMPT_DIR, llm_factory=_factory(llm))
    verdict = asyncio.run(
        reviewer.review(
            user_prompt="any task",
            yaml_text="name: x\n",
        )
    )
    assert verdict.approved is True
    assert verdict.review_used is False
    assert "unreachable" in verdict.reasoning


def test_from_prompt_dir_returns_none_when_file_missing(tmp_path):
    """If reviewer_system.md isn't shipped, ``from_prompt_dir``
    returns None and the composer treats review as disabled. This
    is the back-compat path for installations that don't have the
    reviewer prompt yet."""
    reviewer = WorkflowReviewer.from_prompt_dir(
        tmp_path, llm_factory=_factory(_StubLLM(APPROVED_JSON))
    )
    assert reviewer is None


# ---------------------------------------------------------------------------
# Composer integration
# ---------------------------------------------------------------------------


VALID_SPEC = textwrap.dedent(
    """\
    ```json
    {
      "name": "rev_test",
      "steps": [
        {"id": "rag", "class_name": "RagSynthesisStep"}
      ],
      "links": []
    }
    ```
    """
)


class _SequencedStubLLM:
    """LLM stub that returns canned responses in sequence — used to
    simulate the composer's TWO LLM calls: one for compose, one for
    review. Records each invocation for inspection."""

    def __init__(self, sequence: list[str]) -> None:
        self._sequence = list(sequence)
        self.invocations: list[list] = []

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self._sequence:
            raise AssertionError("stub exhausted its sequence")
        return _Resp(self._sequence.pop(0))


def test_composer_invokes_reviewer_when_enabled():
    """End-to-end: enable_review=True triggers the reviewer call;
    its verdict lands on CompositionSummary."""
    llm = _SequencedStubLLM([VALID_SPEC, APPROVED_JSON])
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    composer._config = composer._config.model_copy(
        update={"enable_review": True, "composer_mode": "spec"}
    )
    # The reviewer is constructed at __init__; flip-after-init
    # requires manual instantiation:
    composer._reviewer = WorkflowReviewer.from_prompt_dir(PROMPT_DIR, llm_factory=lambda **_kw: llm)

    result = asyncio.run(composer.compose("synthesis prompt"))
    # Two LLM calls happened: compose + review.
    assert len(llm.invocations) == 2
    # The verdict landed on the summary.
    verdict = result.composition_summary.review_verdict
    assert verdict is not None
    assert verdict["approved"] is True
    assert verdict["review_used"] is True


def test_composer_surfaces_rejected_concerns_in_review_notes():
    """When the reviewer rejects, the composer adds the reasoning
    + each concern to review_notes so the human approver sees them."""
    llm = _SequencedStubLLM([VALID_SPEC, REJECTED_JSON])
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    composer._config = composer._config.model_copy(
        update={"enable_review": True, "composer_mode": "spec"}
    )
    composer._reviewer = WorkflowReviewer.from_prompt_dir(PROMPT_DIR, llm_factory=lambda **_kw: llm)

    result = asyncio.run(composer.compose("entity prompt"))
    notes = result.composition_summary.review_notes
    assert any("reviewer rejected" in n for n in notes)
    assert any("EntityExtractionStep" in n for n in notes)


def test_composer_skips_reviewer_when_disabled():
    """Default behavior: enable_review=False means zero reviewer
    calls; composition_summary.review_verdict is None."""
    llm = _StubLLM(VALID_SPEC)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    composer._config = composer._config.model_copy(
        update={"enable_review": False, "composer_mode": "spec"}
    )
    result = asyncio.run(composer.compose("any prompt"))
    assert len(llm.invocations) == 1  # only the compose call
    assert result.composition_summary.review_verdict is None


def test_env_var_enables_review(monkeypatch):
    """APECX_COMPOSER_REVIEW=1 flips enable_review at load time."""
    monkeypatch.setenv("APECX_COMPOSER_REVIEW", "1")
    composer = Composer.from_config(DEFAULT_CONFIG)
    assert composer._config.enable_review is True
    assert composer._reviewer is not None
