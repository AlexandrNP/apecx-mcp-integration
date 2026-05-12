"""MemoryReadStep — read prior reflexion-style lessons for the LLM.

Reads from the git-tracked ``MemoryStore`` and formats lessons as a
``critique`` field that ``CodeWriteStep`` consumes directly.
Composes into the self-improving code-writing workflow:

    MemoryReadStep → CodeWriteStep (with critique=<lessons>) → CodeReviewStep → MemoryWriteStep

Silent-failure discipline:
  1. ``input_data`` not a dict → ``ValueError``.
  2. Missing ``spec_id`` AND missing ``spec_keywords`` → ``ValueError``
     (caller must indicate WHICH memory to read).
  3. Store path doesn't exist → returns empty lessons (NOT an error;
     first-time runs against a fresh memory directory are legitimate).
  4. Memory file with newer schema → bubbles up the ``MemoryStore``'s
     fail-fast.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps.memory_store import MemoryStore

log = logging.getLogger(__name__)


class MemoryReadStepConfig(StepConfig):
    """Configuration for MemoryReadStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    memory_dir: str = Field(
        ...,
        description=(
            "Absolute (or workspace-relative) path to the memory "
            "directory's root — the directory that contains "
            "``reflexions/<spec_id>/<id>.json``. Required: there's "
            "no sensible default."
        ),
    )

    limit: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "Maximum number of memory entries to return. Reflexion's "
            "Ω=1–3 cap is the documented sweet spot; larger limits "
            "bloat the LLM prompt with stale lessons."
        ),
    )

    fallback_to_keywords: bool = Field(
        default=True,
        description=(
            "When True (default), if no entries exist for the given "
            "spec_id, fall back to keyword-Jaccard retrieval against "
            "all stored entries' spec_keywords. False disables the "
            "fallback (spec_id-exact only)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class MemoryReadStep(BaseStep):
    """Read prior memory entries and format them as ``critique`` input.

    Expected ``process()`` input::

        {
            "spec_id": "fizzbuzz_v1",     # optional but recommended
            "spec_keywords": ["fizzbuzz", "modulo"],  # used for fallback
            "code_spec": "Write fizzbuzz...",  # passthrough
            "function_name": "fizzbuzz",       # passthrough
            "function_signature": "...",       # passthrough
        }

    Return shape::

        {
            "lessons_count": 2,
            "lessons_text": "Lesson 1: ...\\n\\nLesson 2: ...",
            "critique": "<same as lessons_text — keyed for CodeWriteStep>",
            # passthrough:
            "code_spec": "...",
            "function_name": "...",
            "function_signature": "...",
            "spec_id": "...",
        }

    The ``critique`` key is a deliberate alias so the output dict can
    be wired into ``CodeWriteStep.code_write_input`` directly — that
    step already reads ``critique`` as one of its prompt inputs.
    """

    COMPONENT_TYPE: str = "memory_read_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return MemoryReadStepConfig

    @classmethod
    def extract_component_config(cls, config: MemoryReadStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "memory_dir": config.memory_dir,
            "limit": config.limit,
            "fallback_to_keywords": config.fallback_to_keywords,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: MemoryReadStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        memory_dir = self._resolve_memory_dir(
            component_config["memory_dir"],
            component_config.get("source_path"),
        )
        self._store: MemoryStore = MemoryStore(root=memory_dir)
        self._limit: int = int(component_config["limit"])
        self._fallback: bool = bool(component_config["fallback_to_keywords"])

    @staticmethod
    def _resolve_memory_dir(configured: str, source_path: str | None) -> Path:
        p = Path(configured)
        if p.is_absolute():
            return p
        if source_path:
            return (Path(source_path).resolve().parent / p).resolve()
        return (Path.cwd() / p).resolve()

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"MemoryReadStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )
        if (
            "memory_read_input" in input_data
            and isinstance(input_data["memory_read_input"], dict)
            and "code_spec" not in input_data
        ):
            input_data = input_data["memory_read_input"]

        spec_id = input_data.get("spec_id")
        spec_keywords = input_data.get("spec_keywords") or []
        if not spec_id and not spec_keywords:
            raise ValueError(
                f"MemoryReadStep {self.name!r}: input_data must contain "
                f"either 'spec_id' (str) or 'spec_keywords' (list[str]); "
                f"got keys={sorted(input_data.keys())}"
            )

        entries = await asyncio.to_thread(self._fetch, spec_id, spec_keywords)
        lessons_text = self._format_lessons(entries)

        log.info(
            "MemoryReadStep %r: returned %d lesson(s) for spec_id=%r (fallback_used=%s)",
            self.name,
            len(entries),
            spec_id,
            (not entries and self._fallback and spec_keywords),
        )

        # Passthrough is critical: downstream CodeWriteStep needs the
        # original spec / function_name / signature inputs.
        return {
            "lessons_count": len(entries),
            "lessons_text": lessons_text,
            "critique": lessons_text,  # alias for CodeWriteStep
            "code_spec": input_data.get("code_spec"),
            "function_name": input_data.get("function_name"),
            "function_signature": input_data.get("function_signature"),
            "spec_id": spec_id,
            "spec_keywords": list(spec_keywords),
        }

    def _fetch(self, spec_id: str | None, spec_keywords: list[str]) -> list:
        if spec_id:
            entries = self._store.read_for_spec(spec_id, limit=self._limit)
            if entries:
                return entries
            if not self._fallback:
                return []
        if spec_keywords:
            return self._store.read_by_keywords(spec_keywords=spec_keywords, limit=self._limit)
        return []

    @staticmethod
    def _format_lessons(entries: list) -> str:
        """Format a list of MemoryEntry into a single critique string.

        Empty list → empty string (consumers should treat empty
        critique as "no prior lessons").
        """
        if not entries:
            return ""
        parts: list[str] = []
        for i, e in enumerate(entries, start=1):
            status_marker = (
                "[PASS]"
                if e.status == "pass"
                else "[FAIL]"
                if e.status == "fail"
                else f"[{e.status.upper()}]"
            )
            parts.append(
                f"Prior lesson {i}/{len(entries)} {status_marker} "
                f"(attempt {e.attempt_n}): {e.lesson}"
            )
        return "\n\n".join(parts)


__all__ = ["MemoryReadStep", "MemoryReadStepConfig"]
