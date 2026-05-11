"""B1 — system prompt hardening + candidate-render improvements.

A1 added the framework-rule validator that rejects inline-dict
configs at compose-time. B1 closes the loop on the LLM side:

  1. The system prompt now carries the verbatim framework error
     message (``❌ FRAMEWORK VIOLATION: Inline dict configuration
     not supported for ...``) plus a side-by-side wrong/right
     example. When the LLM has seen the failure signature in its
     prompt, it's more likely to recognize and avoid the shape.

  2. The retrieval candidate block now emits a ``emit_step: |``
     YAML stub per candidate — the literal lines the LLM should
     paste under ``steps:``. The previous block told the LLM
     ``yaml: steps/foo.yml`` and trusted it to assemble the step
     shape from prose. The new block hands over the ready-to-paste
     bytes.

These tests pin both surfaces so a future "simplify the prompt"
edit can't silently remove the failure-signature paste or the
emit_step stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.component_catalog import (
    CatalogComponent,
    SearchHit,
)
from apecx_integration.composition.composer import _render_candidates

PROMPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_prompts"
)
SYSTEM_MD = PROMPTS_DIR / "system.md"


@pytest.fixture(scope="module")
def system_prompt() -> str:
    return SYSTEM_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompt — failure-signature paste
# ---------------------------------------------------------------------------


def test_prompt_carries_verbatim_framework_violation_message(system_prompt):
    """The exact error string from
    nanobrain/nanobrain/core/config/config_base.py:947 must appear
    in the prompt so the LLM learns its signature."""
    assert "FRAMEWORK VIOLATION" in system_prompt
    assert "Inline dict configuration not supported" in system_prompt
    assert "SUPPORTED CLASSES: DataUnit, Link, Trigger" in system_prompt


def test_prompt_carries_side_by_side_wrong_right_example(system_prompt):
    """The wrong/right pair makes the rule concrete — the LLM
    pattern-matches more reliably on a worked example than on a
    prose rule."""
    assert "❌ WRONG" in system_prompt
    assert "✅ CORRECT" in system_prompt
    # The wrong example must show the issues-doc failure shape.
    assert "target_entities" in system_prompt
    # The correct example must use file-path config.
    assert 'config: "steps/entity_extraction.yml"' in system_prompt


# ---------------------------------------------------------------------------
# Candidate block — emit_step stub
# ---------------------------------------------------------------------------


def _hit(name: str, yaml_path: str | None = "steps/x.yml") -> SearchHit:
    return SearchHit(
        component=CatalogComponent(
            id="comp.id",
            name=name,
            description="desc",
            class_path=f"pkg.lib.{name.replace(' ', '')}",
            yaml_path=yaml_path,
            examples=(),
        ),
        score=100,
    )


def test_render_candidates_emits_ready_to_paste_step_stub():
    block = _render_candidates([_hit("FooStep", yaml_path="steps/foo.yml")])
    # The stub must contain the literal class line + literal config
    # line. Removing the emit_step block undoes B1.
    assert "emit_step: |" in block
    assert "class: pkg.lib.FooStep" in block
    assert 'config: "steps/foo.yml"' in block


def test_render_candidates_skips_emit_step_when_no_yaml_path():
    """A candidate without a canonical wrapper has nothing to paste;
    the stub must NOT be emitted (would mislead the LLM into
    inventing a path)."""
    block = _render_candidates([_hit("FooStep", yaml_path=None)])
    assert "emit_step:" not in block


def test_render_candidates_stub_id_is_snake_case():
    """The stub's step_id is derived from the component name; the
    LLM is expected to override it with a task-appropriate name.
    Snake_case mirrors framework conventions."""
    block = _render_candidates([_hit("Entity Extraction-Step", yaml_path="steps/e.yml")])
    # spaces and dashes both collapse to underscores.
    assert "entity_extraction_step:" in block
