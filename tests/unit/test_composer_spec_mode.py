"""SPEC2 — composer's spec_mode end-to-end (stub LLM, no Ollama).

Verifies that when ``composer_mode == "spec"``:

  1. The system prompt swaps to ``spec_system.md``.
  2. The candidate block uses the compact spec-mode format.
  3. The LLM is expected to emit a ```json fenced block.
  4. Parsed JSON is validated as MinimalWorkflowSpec.
  5. Expander produces a workflow_dict that passes A1 validation.
  6. Pydantic + expander errors are repairable parse errors that
     trigger the C1 retry loop.
  7. Defaults still work (monolithic preserved when env var unset).
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


VALID_SPEC_RESPONSE = textwrap.dedent(
    """\
    ```json
    {
      "name": "spec_smoke",
      "description": "two-step synthesis pipeline",
      "steps": [
        {"id": "assemble", "class_name": "SynthesisContextAssemblyStep"},
        {"id": "rag", "class_name": "RagSynthesisStep"}
      ],
      "links": [
        {"source": "workflow_input", "target": "assemble.assembly_input"},
        {"source": "assemble.synthesis_bundle_output", "target": "rag.synthesis_input"},
        {"source": "rag.synthesis_markdown_output", "target": "workflow_output"}
      ]
    }
    ```
    """
)


INVALID_SCHEMA_RESPONSE = textwrap.dedent(
    """\
    ```json
    {
      "name": "missing_required",
      "stes": []
    }
    ```
    """
)


UNKNOWN_CLASS_SPEC = textwrap.dedent(
    """\
    ```json
    {
      "name": "ghost_workflow",
      "steps": [
        {"id": "ghost", "class_name": "TotallyMadeUpStep"}
      ],
      "links": []
    }
    ```
    """
)


class _RecordingResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLLM:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = list(sequence)
        self.invocations: list[list] = []

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self._sequence:
            raise AssertionError("stub LLM exhausted its sequence")
        return _RecordingResponse(self._sequence.pop(0))


def _composer_spec_mode(llm: _StubLLM) -> Composer:
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    composer._config = composer._config.model_copy(  # type: ignore[attr-defined]
        update={"composer_mode": "spec"}
    )
    return composer


def test_spec_mode_succeeds_on_valid_json():
    """The dominant SPEC2 happy path: LLM emits a tiny spec, the
    expander produces a full framework-legal workflow, no retries
    needed, no hallucinations possible (because the LLM never wrote
    a class path)."""
    llm = _StubLLM([VALID_SPEC_RESPONSE])
    composer = _composer_spec_mode(llm)
    result = asyncio.run(composer.compose("synthesis prompt"))
    assert result.composition_summary.compose_retries == 0

    # The workflow YAML carries the expander's deterministic output:
    # canonical class paths, auto_transfer: true on every link,
    # workflow-level data unit blocks auto-scaffolded.
    yaml_text = result.yaml_bytes.decode("utf-8")
    assert "rag_synthesis_step.RagSynthesisStep" in yaml_text
    assert "synthesis_context_assembly_step.SynthesisContextAssemblyStep" in yaml_text
    assert "auto_transfer: true" in yaml_text
    assert "workflow_input" in yaml_text
    assert "workflow_output" in yaml_text


def test_spec_mode_uses_spec_system_prompt_not_monolithic():
    """The system prompt MUST swap when composer_mode=spec.
    Otherwise we'd be sending the wrong instructions to the LLM
    and the test "succeeds" via accident."""
    llm = _StubLLM([VALID_SPEC_RESPONSE])
    composer = _composer_spec_mode(llm)
    asyncio.run(composer.compose("synthesis prompt"))
    first_call = llm.invocations[0]
    system_msg = first_call[0]
    sys_text = getattr(system_msg, "content", "")
    # Cheat-sheet markers from spec_system.md.
    assert "tiny JSON spec" in sys_text or "MinimalWorkflowSpec" in sys_text
    # Monolithic-only markers MUST NOT appear (they'd mean the
    # wrong prompt slipped through).
    assert "Strict output format — emit exactly ONE fenced code block labeled" not in sys_text


def test_spec_mode_pydantic_validation_is_repairable_parse_error():
    """A spec with a typo'd key (Pydantic extra='forbid') surfaces
    as a parse error the C1 retry can repair on the next attempt."""
    llm = _StubLLM([INVALID_SCHEMA_RESPONSE, VALID_SPEC_RESPONSE])
    composer = _composer_spec_mode(llm)
    result = asyncio.run(composer.compose("synthesis prompt"))
    # Retry kicked in, second attempt succeeded.
    assert result.composition_summary.compose_retries == 1
    assert len(llm.invocations) == 2


def test_spec_mode_unknown_class_falls_through_to_retry():
    """A spec whose class_name doesn't exist in the catalog raises
    SpecExpansionError, which the composer wraps as a repairable
    parse error so the retry feedback can name the issue."""
    llm = _StubLLM([UNKNOWN_CLASS_SPEC])
    composer = _composer_spec_mode(llm)
    composer._config = composer._config.model_copy(update={"max_validation_retries": 0})
    with pytest.raises(ComposerResponseError, match="expander could not realize"):
        asyncio.run(composer.compose("ghost prompt"))


def test_default_mode_is_spec_after_adoption_flip():
    """ADOPT (2026-05-12): spec mode became the project default
    after the EXPT-RT roundtrip test reached RUN_COMPLETED on real
    Ollama. This pin documents the new default + catches any
    accidental flip back to monolithic."""
    composer = Composer.from_config(DEFAULT_CONFIG)
    assert composer._config.composer_mode == "spec"


def test_env_var_override_flips_mode_to_spec(monkeypatch):
    """APECX_COMPOSER_MODE=spec must flip the field at load time —
    the operator's single source of truth for switching modes on
    a deployment is the env var; YAML edits are not required."""
    monkeypatch.setenv("APECX_COMPOSER_MODE", "spec")
    composer = Composer.from_config(DEFAULT_CONFIG)
    assert composer._config.composer_mode == "spec"


# --- Sandbox import scan runs in spec mode (2026-07-01 hoist) ----------------------------------
# Before the hoist, _parse_spec_response returned novel_python WITHOUT scanning (the only
# ImportScanner.scan lived on the monolithic branch of _invoke_and_parse), so LLM-authored Python
# bypassed the import whitelist in the SHIPPED default mode. These pin that spec mode is scanned.

_HOSTILE_NOVEL_SPEC = textwrap.dedent(
    """\
    ```json
    {
      "name": "evil_wf",
      "description": "hostile novel python via spec",
      "steps": [{"id": "evil", "class_name": "EvilStep"}],
      "links": [
        {"source": "workflow_input", "target": "evil.step_input"},
        {"source": "evil.step_output", "target": "workflow_output"}
      ],
      "novel_python": {"evil": "import subprocess\\nclass EvilStep:\\n    async def process(self, input_data, **kwargs):\\n        return {}\\n"}
    }
    ```
    """
)

_WHITELISTED_NOVEL_SPEC = textwrap.dedent(
    """\
    ```json
    {
      "name": "ok_wf",
      "description": "whitelisted novel python via spec",
      "steps": [{"id": "reshape", "class_name": "ReshapeStep"}],
      "links": [
        {"source": "workflow_input", "target": "reshape.step_input"},
        {"source": "reshape.step_output", "target": "workflow_output"}
      ],
      "novel_python": {"reshape": "import numpy as np\\nclass ReshapeStep:\\n    async def process(self, input_data, **kwargs):\\n        return {}\\n"}
    }
    ```
    """
)


def test_spec_mode_hostile_novel_python_import_is_scanned():
    """Security regression: a non-whitelisted ``import subprocess`` in a spec's novel_python
    raises ScanViolation from compose() in the DEFAULT spec mode — it used to bypass the scan."""
    from apecx_integration.composition.sandbox import ScanViolation

    llm = _StubLLM([_HOSTILE_NOVEL_SPEC])
    composer = _composer_spec_mode(llm)
    with pytest.raises(ScanViolation):
        asyncio.run(composer.compose("do something with a shell"))


def test_spec_mode_whitelisted_novel_python_not_scan_blocked():
    """A whitelisted import (numpy) in a spec's novel_python does NOT raise ScanViolation in spec
    mode (it may still trip later validators — we assert only that the scan GATE passes)."""
    from apecx_integration.composition.sandbox import ScanViolation

    llm = _StubLLM([_WHITELISTED_NOVEL_SPEC] * 4)
    composer = _composer_spec_mode(llm)
    try:
        asyncio.run(composer.compose("reshape some arrays"))
    except ScanViolation as exc:  # pragma: no cover - the failure we are guarding against
        raise AssertionError(f"whitelisted numpy tripped the scanner: {exc}") from exc
    except Exception:
        pass  # downstream validators (structure/expander) may reject — not the scan gate
