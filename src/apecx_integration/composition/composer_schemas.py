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
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from apecx_integration.composition.differ import StepCategorization

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ComposerConfig(BaseModel):
    """Composer configuration loaded from YAML by ``Composer.from_config``.

    Phase-1 fields are locked (spec §6 P1). Phase-2+ may add optional
    fields (e.g. ``retry_count`` for repair-prompt logic) — add here
    with defaults so existing configs keep loading.

    ``extra='forbid'`` (workspace rule, 2026-04-27 batch 36): a typo
    in ``composer_config.yml`` (e.g. ``library_versoin``) raises at
    config-load instead of silently using whatever the schema chose
    when the missing required field happens to be optional.
    """

    model_config = ConfigDict(extra="forbid")

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
    #
    # ``max_tokens`` budget covers a typical workflow YAML (< 2000
    # tokens) + optional novel Python fence (< 1500 tokens) + margin
    # for composition overhead. Bumping past ~4096 tends to confuse
    # mistral-nemo on the prompt — see ollama integration tests.
    max_tokens: int = Field(default=4096, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    # ``max_retries`` defaults to 0 — that's a development default
    # (fast feedback when prompt-tuning), NOT a production-safe value.
    # In production, a transient LLM 5xx will fail the entire compose
    # request without retry. Operators deploying this composer behind
    # a real workload should override to 2 or 3 via the composer
    # config or APECX_LLM_MAX_RETRIES (audit §5.2).
    max_retries: int = Field(default=0, ge=0)

    # Phase-2 additions — retrieval + sandbox integration.
    component_catalog_paths: list[Path] = Field(default_factory=list)
    retrieval_k: int = Field(default=10, ge=1)
    sandbox_whitelist_path: Path | None = None

    # C1 (2026-05-11): how many times compose() will re-prompt the
    # LLM after a WorkflowValidationError. Default 1 — one repair
    # round is the right tradeoff between recovering from LLM drift
    # and not burning budget on a stuck model. 0 disables retries
    # (useful for cheap regression tests).
    max_validation_retries: int = Field(default=1, ge=0)

    # Phase-4 addition — RAG retrieval swap-in. When set, the composer
    # loads ``ComponentIndex.load(rag_index_dir)`` instead of running
    # the Phase-2 linear-scan ``ComponentCatalog``. When None, falls
    # back to linear scan (the Phase-2 production default).
    #
    # Operators build the index out-of-band via
    # ``scripts/build_rag_index.py``, then point this field at the
    # output directory. Keeping the build step off the compose()
    # path avoids surprising scientists with a one-time ~5s model
    # download + ~1s embedding pass on every cold start.
    rag_index_dir: Path | None = None


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class CompositionSummary:
    """Diff-UX payload (feeds T06).

    Phase-1 counts (``steps_reused`` / ``steps_generated`` /
    ``steps_swapped``) stay for backward compat — existing callers
    read them. T06 adds ``step_categorizations`` (one row per step
    per AP §5.6 AC1) + a richer ``summary_sentence`` format.
    """

    steps_reused: int
    steps_generated: int
    steps_swapped: int
    summary_sentence: str
    review_notes: tuple[str, ...] = ()
    # T06 addition — per-step AP §5.6 categorization. Populated by
    # the composer via ``apecx_integration.composition.differ``.
    # Empty tuple when no steps (backward-compat default).
    step_categorizations: tuple[StepCategorization, ...] = ()
    # C1 (2026-05-11) — how many compose-validate-retry rounds were
    # needed to produce this workflow. 0 means the first LLM
    # response passed the framework validator. > 0 means the LLM
    # emitted at least one framework-illegal workflow and the
    # composer recovered via the structured-feedback retry loop.
    # Used as a regression metric for prompt-quality work (B1+).
    compose_retries: int = 0


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
