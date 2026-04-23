"""Shapes the LLM composer produces + config shape it consumes.

Per ``docs/composer_task_spec.md`` §3, Phase 1.

- ``ComposedWorkflow`` is the frozen dataclass ``compose()`` will
  return once Phase 2 lands. It exists now so downstream consumers
  (T06 diff UX, tests) can type against a stable shape even while
  ``compose()`` still raises ``NotImplementedError``.
- ``CompositionSummary`` is the diff-UX payload shape.
- ``ComposerConfig`` is the pydantic config the ``from_config``
  classmethod loads from a YAML file.

Brutal-truth note: these shapes may still shift during Phase 2
implementation — the spec's §6 allows AC edits as long as this doc
is updated FIRST. If a Phase-2 test needs a field not present here,
add the field here BEFORE changing the test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class ComposerConfig(BaseModel):
    """Composer configuration loaded from YAML by ``Composer.from_config``.

    Phase-1 fields are locked (spec §6 P1). Phase-2+ may add optional
    fields (e.g. ``retry_count`` for repair-prompt logic) — add here
    with defaults so existing configs keep loading.
    """

    # Library pin — embedded in every GeneratedArtifact row (T11 AC3).
    library_version: str

    # LLM backend. Defaults align with apecx_db_integration's
    # APECX_LLM_* env vars (2026-04-22 memo 07 / 2026-04-23 bounds patch).
    llm_model: str = "mistral-small:latest"
    llm_base_url: str = "http://localhost:11434/v1"

    # Prompt files live here; spec AC6 forbids inline prompt strings.
    prompt_dir: Path

    # Phase-2+ caps. Kept here at Phase 1 so ComposerConfig's shape
    # is stable across phases.
    max_tokens: int = Field(default=4096, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_retries: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class CompositionSummary:
    """Diff-UX payload (feeds T06). Phase 1 defines the shape; Phase 2
    populates it; Phase 4 (T06 UX) consumes it.
    """

    steps_reused: int
    steps_generated: int
    steps_swapped: int
    summary_sentence: str
    review_notes: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ComposedWorkflow:
    """What ``Composer.compose()`` will return once Phase 2 lands.

    Frozen + kw_only so partial construction / accidental mutation is
    impossible — the composer produces an artifact; downstream code
    reads it.
    """

    artifact_id: UUID
    yaml_bytes: bytes
    # step_id → source code for Python the composer generated fresh.
    # Empty dict when the workflow is 100% composition (desired per
    # spec AC7's "composition-bias regression").
    novel_python: dict[str, str]
    composition_summary: CompositionSummary
    retrieved_components: tuple[str, ...]
    llm_model: str
    llm_model_version_hash: str


__all__ = [
    "ComposedWorkflow",
    "CompositionSummary",
    "ComposerConfig",
]
