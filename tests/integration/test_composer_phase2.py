"""T-COMP Phase 2 tests — end-to-end ``compose()`` with placeholder LLM.

Per docs/composer_task_spec.md §6 P2 exit criterion: a fixture prompt
produces non-empty ``yaml_bytes`` that loads via
``Workflow.from_config(...)``. These tests exercise the full P2
pipeline WITHOUT a live LLM:

    ComponentCatalog.search → system/user prompt assembly
        → placeholder LLM invoke (canned response)
        → _parse_response fence extraction
        → T13 scanner over novel Python
        → ComposedWorkflow assembly

No network, no Ollama, no ArtifactStore — Phase 3 wires persistence.

**Workspace mocks-policy parity**: the placeholder-LLM stub here is
the "mock" half. The matching "integration" half lives in
``test_composer_phase2_against_ollama.py`` (same commit) which is
operator-run with ``APECX_SKIP_LIVE_LLM`` opt-out.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
import yaml

from apecx_integration.composition.composer import (
    Composer,
    ComposerResponseError,
    _parse_response,
)
from apecx_integration.composition.sandbox import ScanViolation

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)


# ---------------------------------------------------------------------------
# Placeholder LLM stub — minimal surface compose() touches
# ---------------------------------------------------------------------------

class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    """Stand-in for ChatOpenAI. Implements only ``invoke(messages)``.
    Returns a canned response; records the messages for assertions."""

    def __init__(self, canned: str):
        self.canned = canned
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _PlaceholderResponse(self.canned)


def _make_llm_factory(canned: str) -> tuple[list, callable]:
    """Build an llm_factory callable that returns a fresh placeholder
    LLM each invocation. Returns (captured_llms, factory).
    """
    captured: list[_PlaceholderLLM] = []

    def _factory(**_kwargs):
        llm = _PlaceholderLLM(canned)
        captured.append(llm)
        return llm

    return captured, _factory


# ---------------------------------------------------------------------------
# Happy-path: LLM emits a valid yaml fence; compose returns ComposedWorkflow
# ---------------------------------------------------------------------------

HAPPY_PATH_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: test_workflow
    description: "Test workflow produced by placeholder LLM."
    version: "0.1.0"
    steps:
      entity_extraction:
        class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
        config: "steps/entity_extraction.yml"
      result_ranking:
        class: "nanobrain.library.workflows.viral_protein_analysis.steps.result_collection_step.ResultCollectionStep"
        config: "steps/result_ranking.yml"
    links:
      step1_to_step7:
        class: "nanobrain.core.link.DirectLink"
        config:
          link_type: "direct"
          source: "entity_extraction.entity_candidates_output"
          target: "result_ranking.enriched_results_input"
    ```
    """
)


def test_compose_happy_path_returns_composed_workflow():
    """compose(prompt) with a canned LLM response yields a
    ComposedWorkflow whose yaml_bytes is parseable and names the
    expected steps."""
    captured, factory = _make_llm_factory(HAPPY_PATH_RESPONSE)
    composer = Composer.from_config(DEFAULT_CONFIG)
    # Inject the placeholder factory. This is the seam the class exposes
    # for exactly this purpose — the production path uses the default
    # apecx_db_integration-backed factory.
    composer._llm_factory = factory

    result = asyncio.run(composer.compose("find entities then rank them"))

    assert result.yaml_bytes, "expected non-empty yaml_bytes"
    workflow = yaml.safe_load(result.yaml_bytes.decode("utf-8"))
    assert workflow["name"] == "test_workflow"
    assert set(workflow["steps"].keys()) == {"entity_extraction", "result_ranking"}
    assert result.novel_python == {}, (
        "happy-path prompt should not elicit novel python (composition bias)"
    )
    assert len(captured) == 1, "expected exactly one LLM invocation"


def test_compose_passes_system_and_user_messages_to_llm():
    """The LLM call must include BOTH the system prompt (3-file
    concatenation) AND the user prompt (task + candidate components).
    If the system prompt is empty, AC6 is vacuously true in a bad way.
    """
    captured, factory = _make_llm_factory(HAPPY_PATH_RESPONSE)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    asyncio.run(composer.compose("find entities"))

    messages = captured[0].calls[0]
    assert len(messages) == 2, f"expected SystemMessage + HumanMessage, got {len(messages)}"
    system_text = messages[0].content
    user_text = messages[1].content
    # System prompt carries all three files
    assert "You are a workflow composer" in system_text
    assert "Prefer composition" in system_text
    assert "novel Python" in system_text
    # User prompt carries the task + candidates
    assert "find entities" in user_text
    assert "## Available library components" in user_text
    # And at least one candidate surfaced (we have 9 in the catalog;
    # "find entities" matches the entity_extraction step).
    assert "entity_extraction" in user_text


def test_compose_retrieval_hits_appear_in_retrieved_components():
    """The ComposedWorkflow.retrieved_components is the audit trail
    of what the catalog surfaced — review-UX needs it for "why did
    the composer choose these components?"
    """
    captured, factory = _make_llm_factory(HAPPY_PATH_RESPONSE)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    result = asyncio.run(composer.compose("extract biomedical entities from query"))
    assert "1" in result.retrieved_components  # step_id "1" == entity_extraction


# ---------------------------------------------------------------------------
# Novel-Python path: LLM emits BOTH yaml and novel_python fences
# ---------------------------------------------------------------------------

NOVEL_PYTHON_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: test_workflow
    description: "Workflow with one novel step."
    version: "0.1.0"
    steps:
      custom_step:
        class: "generated.CustomStep"
        config: {}
    links: {}
    ```

    ```novel_python
    custom_step: |
      import json
      from dataclasses import dataclass

      @dataclass
      class CustomStep:
          async def process(self, input_data, **kwargs):
              return {"echo": json.dumps(input_data)}
    ```
    """
)


def test_compose_extracts_novel_python_when_llm_emits_it():
    captured, factory = _make_llm_factory(NOVEL_PYTHON_RESPONSE)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    result = asyncio.run(composer.compose("do something custom"))
    assert "custom_step" in result.novel_python
    assert "async def process" in result.novel_python["custom_step"]
    assert result.composition_summary.steps_generated == 1


# ---------------------------------------------------------------------------
# T13 scanner integration: novel Python with banned construct → reject
# ---------------------------------------------------------------------------

BANNED_NOVEL_PYTHON_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: unsafe_workflow
    description: "Workflow with a step that uses exec()."
    version: "0.1.0"
    steps:
      evil_step:
        class: "generated.EvilStep"
        config: {}
    links: {}
    ```

    ```novel_python
    evil_step: |
      import json

      class EvilStep:
          async def process(self, input_data, **kwargs):
              code = "result = 2 + 2"
              exec(code)  # banned by T13 scanner
              return {"result": 4}
    ```
    """
)


def test_compose_rejects_novel_python_with_banned_constructs():
    captured, factory = _make_llm_factory(BANNED_NOVEL_PYTHON_RESPONSE)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    with pytest.raises(ScanViolation, match="exec"):
        asyncio.run(composer.compose("do something evil"))


NON_WHITELISTED_NOVEL_PYTHON_RESPONSE = textwrap.dedent(
    """\
    ```yaml
    name: shell_workflow
    description: "Workflow that tries to shell out."
    version: "0.1.0"
    steps:
      shell_step:
        class: "generated.ShellStep"
        config: {}
    links: {}
    ```

    ```novel_python
    shell_step: |
      import subprocess  # not on the sandbox whitelist

      class ShellStep:
          async def process(self, input_data, **kwargs):
              subprocess.run(["echo", "hi"], check=True)
              return {}
    ```
    """
)


def test_compose_rejects_novel_python_with_non_whitelisted_imports():
    captured, factory = _make_llm_factory(NON_WHITELISTED_NOVEL_PYTHON_RESPONSE)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    with pytest.raises(ScanViolation, match="subprocess"):
        asyncio.run(composer.compose("shell out"))


# ---------------------------------------------------------------------------
# Malformed LLM responses: compose must raise ComposerResponseError
# ---------------------------------------------------------------------------

def test_compose_raises_when_llm_emits_no_yaml_fence():
    captured, factory = _make_llm_factory("Sure, here's your workflow: ...")
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    with pytest.raises(ComposerResponseError, match="no .*yaml.*fenced block"):
        asyncio.run(composer.compose("anything"))


def test_compose_raises_when_yaml_fence_is_unparseable():
    # The yaml is syntactically broken (tab then space plus dangling ::).
    bad = textwrap.dedent(
        """\
        ```yaml
        name: broken
          \t::  bad indent
        ```
        """
    )
    captured, factory = _make_llm_factory(bad)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    with pytest.raises(ComposerResponseError):
        asyncio.run(composer.compose("whatever"))


def test_compose_raises_when_yaml_is_not_top_level_mapping():
    """An LLM that emits ``[a, b, c]`` as a yaml list is unusable —
    workflows must be top-level mappings."""
    bad = textwrap.dedent(
        """\
        ```yaml
        - step1
        - step2
        ```
        """
    )
    captured, factory = _make_llm_factory(bad)
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = factory

    with pytest.raises(ComposerResponseError, match="mapping at top level"):
        asyncio.run(composer.compose("whatever"))


# ---------------------------------------------------------------------------
# _parse_response — unit coverage on the fence extractor
# ---------------------------------------------------------------------------

def test_parse_response_handles_prose_around_fences():
    """LLMs often add a sentence of prose before the yaml. Our parser
    tolerates it — we only care about the fenced blocks."""
    content = textwrap.dedent(
        """\
        Sure, here's the workflow you asked for:

        ```yaml
        name: ok
        ```

        Let me know if you need anything else.
        """
    )
    yaml_text, novel_python = _parse_response(content)
    assert "name: ok" in yaml_text
    assert novel_python == {}


def test_parse_response_picks_first_yaml_when_multiple_emitted():
    content = textwrap.dedent(
        """\
        ```yaml
        name: first
        ```

        ```yaml
        name: second
        ```
        """
    )
    yaml_text, _ = _parse_response(content)
    assert "first" in yaml_text
    assert "second" not in yaml_text


def test_parse_response_rejects_novel_python_wrong_shape():
    """novel_python must be a mapping step_id → source string."""
    content = textwrap.dedent(
        """\
        ```yaml
        name: x
        ```

        ```novel_python
        - not a mapping
        - also not a mapping
        ```
        """
    )
    with pytest.raises(ComposerResponseError, match="must be a mapping"):
        _parse_response(content)
