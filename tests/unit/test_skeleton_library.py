"""SKEL — skeleton library + composer integration.

Tests:
  1. SkeletonLibrary loads YAML files into validated Skeleton
     records. Malformed entries skipped with warning, not crash.
  2. The composer wires the library into __init__.
  3. When LLM emits `{"skeleton": "synthesis_pipeline"}`, the
     composer expands it via the pre-authored spec — no LLM
     hallucination surface for the workflow topology.
  4. Unknown skeleton names raise a repairable parse error so
     C1 retry can guide the LLM to a real name.
  5. Empty library doesn't poison the spec_system prompt.

The three shipped skeletons (synthesis_pipeline, entity_extraction_only,
pathogen_bvbrc_match) are exercised against the real composer fixture.
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
)
from apecx_integration.composition.skeletons import (
    SkeletonLibrary,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
SHIPPED_SKELETONS = REPO_ROOT / "src" / "apecx_integration" / "composition" / "skeletons"


# ---------------------------------------------------------------------------
# Library loader
# ---------------------------------------------------------------------------


def test_skeleton_library_loads_shipped_files():
    lib = SkeletonLibrary.from_dir(SHIPPED_SKELETONS)
    names = lib.names()
    # Three skeletons authored as the SKEL starter set.
    assert "synthesis_pipeline" in names
    assert "entity_extraction_only" in names
    assert "pathogen_bvbrc_match" in names


def test_skeleton_library_validates_embedded_spec(tmp_path):
    """A skeleton whose embedded spec is structurally invalid must
    fail to load (Pydantic catches it at load), not silently
    succeed. Author errors get caught early."""
    bad = tmp_path / "bad.yml"
    bad.write_text(
        yaml.safe_dump(
            {
                "name": "bad",
                "description": "wrong shape",
                "spec": {"name": "x", "steps": "should be a list"},
            }
        )
    )
    # The library swallows + logs malformed skeletons rather than
    # crashing — workspace stability rule.
    lib = SkeletonLibrary.from_dir(tmp_path)
    assert lib.get("bad") is None
    assert lib.names() == []


def test_skeleton_library_dedups_by_name(tmp_path):
    body = {
        "name": "dup",
        "description": "first",
        "spec": {"name": "dup_spec", "steps": [], "links": []},
    }
    (tmp_path / "a.yml").write_text(yaml.safe_dump(body))
    body["description"] = "second"
    (tmp_path / "b.yml").write_text(yaml.safe_dump(body))
    lib = SkeletonLibrary.from_dir(tmp_path)
    # First-wins (lexicographic by filename).
    assert lib.get("dup") is not None
    assert lib.get("dup").description == "first"


def test_empty_directory_returns_empty_library(tmp_path):
    lib = SkeletonLibrary.from_dir(tmp_path)
    assert lib.names() == []
    assert lib.render_prompt_block() == ""


# ---------------------------------------------------------------------------
# Prompt block rendering
# ---------------------------------------------------------------------------


def test_render_prompt_block_lists_each_skeleton():
    lib = SkeletonLibrary.from_dir(SHIPPED_SKELETONS)
    block = lib.render_prompt_block()
    assert "## Available skeletons" in block
    assert "synthesis_pipeline" in block
    assert "entity_extraction_only" in block
    assert "pathogen_bvbrc_match" in block


def test_composer_init_loads_skeleton_library():
    """The composer instantiates the library at __init__ time so
    every compose() call has it available without re-loading. A
    sustained nonzero size means operators see the skeletons in
    the prompt."""
    composer = Composer.from_config(DEFAULT_CONFIG)
    assert len(composer._skeleton_library.names()) >= 3


# ---------------------------------------------------------------------------
# Spec-mode composer integration
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLLM:
    def __init__(self, content: str) -> None:
        self._content = content
        self.invocations: list[list] = []

    def invoke(self, messages):
        self.invocations.append(list(messages))
        return _Resp(self._content)


def _spec_composer(llm: _StubLLM) -> Composer:
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: llm  # noqa: SLF001
    # Spec mode is now default but make explicit for test clarity.
    composer._config = composer._config.model_copy(update={"composer_mode": "spec"})
    return composer


SKELETON_SHORTHAND = textwrap.dedent(
    """\
    ```json
    {"skeleton": "synthesis_pipeline"}
    ```
    """
)

UNKNOWN_SKELETON = textwrap.dedent(
    """\
    ```json
    {"skeleton": "totally_made_up_skeleton"}
    ```
    """
)

VALID_FULL_SPEC = textwrap.dedent(
    """\
    ```json
    {
      "name": "fallback_spec",
      "steps": [
        {"id": "rag", "class_name": "RagSynthesisStep"}
      ],
      "links": []
    }
    ```
    """
)


def test_skeleton_shorthand_expands_to_full_workflow():
    """The whole point of SKEL: the LLM emits the smallest possible
    JSON, the composer fills in the rest deterministically."""
    llm = _StubLLM(SKELETON_SHORTHAND)
    composer = _spec_composer(llm)
    result = asyncio.run(composer.compose("synthesis prompt"))
    assert result.composition_summary.compose_retries == 0
    # The expander wrote canonical class paths for both library
    # components in the skeleton — no LLM-side hallucination
    # opportunity.
    yaml_text = result.yaml_bytes.decode("utf-8")
    assert "synthesis_context_assembly_step.SynthesisContextAssemblyStep" in yaml_text
    assert "rag_synthesis_step.RagSynthesisStep" in yaml_text


def test_unknown_skeleton_name_is_repairable_retry():
    """If the LLM emits a non-existent skeleton name, the composer
    raises a parse error the C1 retry can guide. The error message
    MUST list the available skeleton names so the retry feedback
    is actionable."""
    llm = _StubLLM(UNKNOWN_SKELETON)
    composer = _spec_composer(llm)
    composer._config = composer._config.model_copy(update={"max_validation_retries": 0})
    with pytest.raises(ComposerResponseError) as excinfo:
        asyncio.run(composer.compose("synthesis prompt"))
    msg = str(excinfo.value)
    assert "totally_made_up_skeleton" in msg
    # Available list appears so the LLM can pick a real one.
    assert "synthesis_pipeline" in msg


def test_spec_system_prompt_advertises_skeletons():
    """The system prompt MUST list available skeletons so the LLM
    knows which names are valid. Without this, the LLM would have
    no way to discover the shorthand."""
    llm = _StubLLM(VALID_FULL_SPEC)
    composer = _spec_composer(llm)
    asyncio.run(composer.compose("prompt"))
    system_msg = llm.invocations[0][0]
    sys_text = getattr(system_msg, "content", "")
    assert "Available skeletons" in sys_text
    assert "synthesis_pipeline" in sys_text


def test_full_spec_still_works_when_skeletons_exist():
    """The skeleton shorthand is OPTIONAL. The LLM can still emit a
    full MinimalWorkflowSpec when no skeleton fits. This pin ensures
    the introduction of skeletons didn't break the underlying
    spec-mode path."""
    llm = _StubLLM(VALID_FULL_SPEC)
    composer = _spec_composer(llm)
    result = asyncio.run(composer.compose("prompt"))
    assert result.composition_summary.compose_retries == 0
    yaml_text = result.yaml_bytes.decode("utf-8")
    assert "rag_synthesis_step.RagSynthesisStep" in yaml_text
