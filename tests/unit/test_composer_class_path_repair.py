"""CPR — end-to-end test of the compose() pipeline auto-repair.

Pins the behavior that closes the dominant LLM hallucination shape:

  LLM emits ``pkg.steps.rag_synthesis.RagSynthesisStep`` (suffix drop);
  catalog has   ``pkg.steps.rag_synthesis_step.RagSynthesisStep``;
  compose() auto-corrects, persists the repair on the summary,
  validation passes (no retry needed).

Uses a stub LLM so we exercise the pipeline without an Ollama call.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from apecx_integration.composition.composer import Composer

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"


# Issues-doc / E2E observation: mistral-nemo emits this exact pattern.
# Module path drops the ``_step`` suffix; class name is correct.
SUFFIX_DROP_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: cpr_suffix_drop_test
    description: leaf-match repair fixture
    version: "0.1.0"
    steps:
      rag_synth:
        class: "apecx_integration.composition.steps.rag_synthesis.RagSynthesisStep"
        config: "steps/rag_synthesis.yml"
    links: {}
    ```
    """
)


class _RecordingResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLLM:
    def __init__(self, content: str) -> None:
        self._content = content
        self.invocations: list[list] = []

    def invoke(self, messages):
        self.invocations.append(list(messages))
        return _RecordingResponse(self._content)


def _composer_with(llm: _StubLLM) -> Composer:
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    return composer


def test_suffix_drop_class_path_auto_repaired():
    """The dominant LLM hallucination shape repairs without a retry."""
    llm = _StubLLM(SUFFIX_DROP_RESPONSE)
    composer = _composer_with(llm)
    result = asyncio.run(composer.compose("smoke prompt for cpr"))

    # No retry was needed — the repair fixed the workflow before
    # the validator could fail it.
    assert result.composition_summary.compose_retries == 0
    assert len(llm.invocations) == 1

    # The repair MUST be recorded so reviewers see it.
    repairs = result.composition_summary.class_path_repairs
    assert len(repairs) == 1
    step_id, emitted, resolved = repairs[0]
    assert step_id == "rag_synth"
    assert emitted == ("apecx_integration.composition.steps.rag_synthesis.RagSynthesisStep")
    assert resolved == ("apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep")

    # The persisted YAML must carry the corrected class path so the
    # artifact and the runtime workflow agree.
    yaml_text = result.yaml_bytes.decode("utf-8")
    assert "rag_synthesis_step.RagSynthesisStep" in yaml_text
    # And the broken path must NOT appear in the persisted YAML
    # (otherwise the runtime would still try to import it).
    assert "rag_synthesis.RagSynthesisStep" not in yaml_text.replace(
        "rag_synthesis_step.RagSynthesisStep", ""
    )


HALLUCINATED_CLASS_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: cpr_truly_novel_test
    steps:
      ghost:
        class: "pkg.invented.path.CompletelyMadeUpStep"
        config: "steps/ghost.yml"
    links: {}
    ```
    """
)


def test_truly_invented_class_falls_through_to_validator():
    """A class whose leaf doesn't match ANY catalog entry must NOT
    get auto-corrected — the validator's step_class_unresolvable
    fires (then C1 retry, then exhaustion).

    The repair pass MUST stay surgical: only leaf-match-unique
    cases get rewritten. Otherwise we'd silently substitute the
    wrong class for an invented one.
    """
    from apecx_integration.composition.workflow_validator import (
        WorkflowValidationError,
    )

    llm = _StubLLM(HALLUCINATED_CLASS_RESPONSE)
    composer = _composer_with(llm)
    # Disable retries so we surface the validator error directly.
    composer._config = composer._config.model_copy(  # type: ignore[attr-defined]
        update={"max_validation_retries": 0}
    )
    with pytest.raises(WorkflowValidationError) as excinfo:
        asyncio.run(composer.compose("smoke prompt"))
    rule_ids = [v.rule_id for v in excinfo.value.violations]
    assert "step_class_unresolvable" in rule_ids
