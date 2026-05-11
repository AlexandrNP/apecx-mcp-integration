"""E2E-OLLAMA — exercise A1 + C1 + B1 against a real LLM.

This test verifies the structured-feedback MACHINERY around the
composer, not the LLM's quality. With a small model like
mistral-nemo, even good prompts can produce framework-illegal
workflows that exceed the retry budget — that's a real-world
condition the diagnostic surface must handle correctly.

What we assert about the machinery (regardless of LLM outcome):

  - compose() returns either a ComposedWorkflow OR raises a
    structured ``WorkflowValidationError`` / ``ComposerResponseError``.
    No unstructured tracebacks.
  - ``composition_summary.compose_retries`` is observable on
    success; on failure, the violations have stable rule_ids.
  - When a retry happens, a WARNING line names the rule_ids OR
    the parse-error string so operators can correlate.
  - The C2 runtime_violations channel is reached when load fails
    (covered by the unit suite — here we just check that A1
    catches everything it should).

This file does NOT assert "compose succeeds." That would couple
CI to a specific model's behavior on a specific prompt. The
existing ``test_t01_ac1_against_ollama.py`` is the strict success
gate for AC1; this file is the structured-feedback gate.

Run under the venv:

    APECX_LLM_BASE_URL=http://localhost:11434/v1 \\
    APECX_LLM_MODEL=mistral-nemo:latest \\
    APECX_LLM_TEMPERATURE=0.0 APECX_LLM_MAX_TOKENS=2048 \\
    PYTHONPATH=src .venv/bin/python -m pytest \\
      tests/integration/test_composer_validator_e2e_against_ollama.py -v -s
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

try:
    import nanobrain.core.workflow  # noqa: F401

    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"


def _llm_reachable() -> bool:
    base = os.environ.get("APECX_LLM_BASE_URL") or "http://localhost:11434/v1"
    probe = base[:-3] + "/api/tags" if base.endswith("/v1") else base.rstrip("/") + "/api/tags"
    try:
        r = httpx.get(probe, timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


SKIP_DEPS = "nanobrain not importable — run under the venv"
SKIP_LLM = (
    "LLM not reachable — set APECX_LLM_BASE_URL and make sure ollama serves the requested model"
)


HAPPY_PATH_PROMPT = (
    "Synthesize a grounded markdown answer from a biomedical query "
    "using the SynthesisContextAssemblyStep followed by RagSynthesisStep. "
    "The two steps already exist in the library; just wire them."
)
PARAMETER_OVERRIDE_PROMPT = (
    "Extract entities from a free-text query targeting customer, "
    "product, and feature entity types with a high-confidence "
    "threshold and small candidate list, then proceed to the next "
    "library step."
)


@dataclass
class _CaptureOutcome:
    """Diagnostic record of what compose() actually did."""

    succeeded: bool
    compose_retries: int = 0
    summary_sentence: str = ""
    failure_kind: str = ""
    rule_ids: tuple[str, ...] = ()
    exception_text: str = ""


def _drive_composer(prompt: str, caplog) -> _CaptureOutcome:
    """Run compose() under caplog and return a structured outcome.

    Whether the LLM produced a valid workflow or not, we surface
    every observable signal the machinery is supposed to emit.
    """
    from apecx_integration.composition.composer import (
        Composer,
        ComposerResponseError,
    )
    from apecx_integration.composition.workflow_validator import (
        WorkflowValidationError,
    )

    composer = Composer.from_config(COMPOSER_CONFIG)
    with caplog.at_level(logging.WARNING, logger="apecx_integration.composition.composer"):
        try:
            composed = asyncio.run(composer.compose(prompt))
        except WorkflowValidationError as exc:
            return _CaptureOutcome(
                succeeded=False,
                failure_kind="WorkflowValidationError",
                rule_ids=tuple(v.rule_id for v in exc.violations),
                exception_text=str(exc)[:500],
            )
        except ComposerResponseError as exc:
            return _CaptureOutcome(
                succeeded=False,
                failure_kind="ComposerResponseError",
                exception_text=str(exc)[:500],
            )
    return _CaptureOutcome(
        succeeded=True,
        compose_retries=composed.composition_summary.compose_retries,
        summary_sentence=composed.composition_summary.summary_sentence,
    )


@pytest.mark.skipif(not _DEPS_OK, reason=SKIP_DEPS)
@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_machinery_exercises_loop_correctly_on_happy_path_prompt(caplog):
    """Drive compose() with a constrained "use these two steps" prompt
    and assert the loop machinery is correct, regardless of LLM
    outcome:

      - If compose() succeeded: compose_retries observable + summary
        non-empty + (when retries > 0) a retry WARNING was logged.
      - If compose() failed: it raised a structured exception class
        (NOT a bare RuntimeError / Exception); rule_ids are non-empty
        for WorkflowValidationError; for ComposerResponseError the
        message is a recognized failure shape.
    """
    outcome = _drive_composer(HAPPY_PATH_PROMPT, caplog)
    _assert_loop_machinery_correct(outcome, caplog)
    print(f"\n[E2E happy] {outcome}")


@pytest.mark.skipif(not _DEPS_OK, reason=SKIP_DEPS)
@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_machinery_exercises_loop_correctly_on_parameter_override_prompt(caplog):
    """Same machinery assertions on the parameter-override prompt
    that historically triggered the issues-doc inline-dict failure.

    This test does NOT assert "the LLM repairs within budget." A
    real production LLM may need a bigger budget or a stronger
    model. The machinery (catch → retry-with-feedback → exhaust →
    raise-structured) is what we pin here."""
    outcome = _drive_composer(PARAMETER_OVERRIDE_PROMPT, caplog)
    _assert_loop_machinery_correct(outcome, caplog)
    print(f"\n[E2E param-override] {outcome}")


def _assert_loop_machinery_correct(outcome: _CaptureOutcome, caplog) -> None:
    """Universal machinery assertions.

    Either outcome is valid; we just check the machinery's exposed
    signals are correct for whichever path was taken.
    """
    if outcome.succeeded:
        # Headline metric observable.
        assert outcome.compose_retries >= 0
        # Summary sentence non-empty + carries the AP §5.6 format.
        assert outcome.summary_sentence
        assert "step(s)" in outcome.summary_sentence
        # When at least one retry happened, a WARNING line names the
        # rule_ids OR the parse-error class so operators can correlate
        # with the trigger.
        if outcome.compose_retries > 0:
            warnings = [
                r
                for r in caplog.records
                if "Composer validation failed" in r.getMessage()
                or "Composer parse failed" in r.getMessage()
            ]
            assert warnings, (
                f"compose_retries={outcome.compose_retries} but no "
                "retry WARNING was logged — diagnostic surface is silent"
            )
        return
    # Failure path: must be a structured class, not a bare exception.
    assert outcome.failure_kind in (
        "WorkflowValidationError",
        "ComposerResponseError",
    ), (
        "compose() raised something the loop is supposed to handle "
        f"structurally; got failure_kind={outcome.failure_kind!r}, "
        f"exception_text={outcome.exception_text!r}"
    )
    if outcome.failure_kind == "WorkflowValidationError":
        assert outcome.rule_ids, (
            "WorkflowValidationError must carry at least one rule_id; "
            "an empty violations tuple means the validator misclassified "
            "the failure"
        )
        # Every rule_id must be one A1 knows about (no typos in the
        # rule_id catalog).

        # The rule_id namespace is the union of every rule_id any
        # validator helper can emit. Inspect both helpers via a
        # known-bad workflow and collect rule_ids.
        # Simpler: just assert the rule_ids include known prefixes.
        known_prefixes = (
            "workflow_",
            "step_",
            "link_",
            "steps_",
            "links_",
        )
        for rid in outcome.rule_ids:
            assert any(rid.startswith(p) for p in known_prefixes), (
                f"unknown rule_id {rid!r} — possible typo or namespace drift"
            )
    elif outcome.failure_kind == "ComposerResponseError":
        # The message should name a recognized parse failure class.
        # We accept either the shape-error path or the unparseable
        # path — both are real failure modes.
        text = outcome.exception_text.lower()
        recognized = any(
            marker in text
            for marker in (
                "must be a mapping at top level",
                "yaml block failed to parse",
                "no ```yaml",
                "empty or non-string",
            )
        )
        assert recognized, (
            f"ComposerResponseError text does not match any known "
            f"failure shape: {outcome.exception_text!r}"
        )
