"""MemoryWriteStep — persist a reflexion-style lesson after a cycle.

Reads the review verdict + verifier result (when present) and writes
a new memory entry. Composes into the self-improving code-writing
workflow as the LAST step.

Silent-failure discipline:
  1. ``input_data`` not a dict → ``ValueError``.
  2. Missing ``spec_id`` → ``ValueError``.
  3. No ``lesson`` AND no ``review_verdict`` to derive one from →
     ``ValueError`` (a write call must have *something* to write).
  4. Store.write() returning None (gate skipped) → return
     ``{"written": False, "reason": "..."}`` — NOT an error, but
     surfaces honestly so callers know nothing landed.
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


class MemoryWriteStepConfig(StepConfig):
    """Configuration for MemoryWriteStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    memory_dir: str = Field(..., description="Memory store root directory.")

    skip_if_restatement: bool = Field(
        default=True,
        description=(
            "When True (default), skip writing entries whose lesson "
            "is a near-duplicate of the newest entry for the same "
            "spec_id (keyword + lesson Jaccard > 0.7). False forces "
            "every cycle to write (useful for tests; not recommended "
            "for production)."
        ),
    )

    min_lesson_chars: int = Field(
        default=40,
        ge=0,
        description=(
            "Minimum lesson length to accept (after strip). Shorter "
            "lessons are low-signal restatements; rejected by default. "
            "Set 0 to disable (NOT recommended in production)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class MemoryWriteStep(BaseStep):
    """Write a reflexion-style memory entry from a cycle's result.

    Expected ``process()`` input — at minimum::

        {"spec_id": "fizzbuzz_v1", "lesson": "<free text>"}

    Richer input the step uses to derive a lesson + classification::

        {
            "spec_id": "fizzbuzz_v1",
            "attempt_n": 2,             # optional, defaults to 1
            "code_source": "...",       # optional, used for metadata
            "code_spec": "...",
            "function_name": "fizzbuzz",
            "function_signature": "...",
            "spec_keywords": [...],     # optional
            "review_verdict": {         # optional, from CodeReviewStep
                "approved": False,
                "reasoning": "...",
                "concerns": [...],
                "suggestions": [...],
            },
            "exec_result": {            # optional, from IsolatedPyExecStep
                "exec_succeeded": False,
                "stderr": "...",
                ...
            },
            "lesson": "<override>",     # optional, beats derivation
        }

    Return::

        {
            "written": True | False,
            "reason": "<why skipped, when applicable>",
            "entry_path": "/abs/path/to/entry.json" | None,
            "lesson_used": "<final lesson text>",
            "status_classified": "pass" | "fail" | "partial",
        }
    """

    COMPONENT_TYPE: str = "memory_write_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return MemoryWriteStepConfig

    @classmethod
    def extract_component_config(cls, config: MemoryWriteStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "memory_dir": config.memory_dir,
            "skip_if_restatement": config.skip_if_restatement,
            "min_lesson_chars": config.min_lesson_chars,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: MemoryWriteStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        memory_dir = self._resolve_memory_dir(
            component_config["memory_dir"],
            component_config.get("source_path"),
        )
        self._store: MemoryStore = MemoryStore(root=memory_dir)
        self._skip_if_restatement: bool = bool(component_config["skip_if_restatement"])
        self._min_lesson_chars: int = int(component_config["min_lesson_chars"])

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
                f"MemoryWriteStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )
        if (
            "memory_write_input" in input_data
            and isinstance(input_data["memory_write_input"], dict)
            and "spec_id" not in input_data
        ):
            input_data = input_data["memory_write_input"]

        spec_id = input_data.get("spec_id")
        if not isinstance(spec_id, str) or not spec_id.strip():
            raise ValueError(
                f"MemoryWriteStep {self.name!r}: input_data must contain "
                f"a non-empty 'spec_id' (str); got "
                f"{type(spec_id).__name__}={spec_id!r}"
            )

        lesson = self._derive_lesson(input_data)
        if not lesson:
            raise ValueError(
                f"MemoryWriteStep {self.name!r}: no lesson to write. "
                f"Provide an explicit 'lesson' field OR a "
                f"'review_verdict' dict with reasoning/concerns/suggestions. "
                f"Got keys={sorted(input_data.keys())}"
            )

        status = self._classify_status(input_data)
        attempt_n = int(input_data.get("attempt_n", 1))
        spec_keywords = list(input_data.get("spec_keywords") or [])
        failure_keywords = list(input_data.get("failure_keywords") or [])
        if status == "fail" and not failure_keywords:
            failure_keywords = self._derive_failure_keywords(input_data)

        metadata = self._build_metadata(input_data)

        entry_path = await asyncio.to_thread(
            self._store.write,
            spec_id=spec_id,
            attempt_n=attempt_n,
            status=status,
            lesson=lesson,
            failure_keywords=failure_keywords,
            spec_keywords=spec_keywords,
            metadata=metadata,
            skip_if_restatement=self._skip_if_restatement,
            min_lesson_chars=self._min_lesson_chars,
        )

        if entry_path is None:
            log.info(
                "MemoryWriteStep %r: skipped (restatement or low-signal) for spec_id=%r",
                self.name,
                spec_id,
            )
            return {
                "written": False,
                "reason": (
                    "skipped — either lesson too short OR near-duplicate "
                    "of the newest entry for this spec_id"
                ),
                "entry_path": None,
                "lesson_used": lesson,
                "status_classified": status,
            }

        log.info(
            "MemoryWriteStep %r: wrote %s (spec_id=%r, status=%s, lesson=%d chars)",
            self.name,
            entry_path,
            spec_id,
            status,
            len(lesson),
        )
        return {
            "written": True,
            "reason": "",
            "entry_path": str(entry_path),
            "lesson_used": lesson,
            "status_classified": status,
        }

    @staticmethod
    def _derive_lesson(input_data: dict[str, Any]) -> str:
        explicit = input_data.get("lesson")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        verdict = input_data.get("review_verdict")
        if isinstance(verdict, dict):
            parts: list[str] = []
            reasoning = verdict.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                parts.append(reasoning.strip())
            concerns = verdict.get("concerns") or []
            if isinstance(concerns, list) and concerns:
                parts.append("Concerns: " + "; ".join(str(c) for c in concerns[:3]))
            suggestions = verdict.get("suggestions") or []
            if isinstance(suggestions, list) and suggestions:
                parts.append("Suggestions: " + "; ".join(str(s) for s in suggestions[:3]))
            if parts:
                return " ".join(parts)
        exec_result = input_data.get("exec_result")
        if isinstance(exec_result, dict) and exec_result.get("exec_succeeded") is False:
            err = (exec_result.get("stderr") or "").strip()
            if err:
                return f"Runtime failure: {err[:300]}"
        return ""

    @staticmethod
    def _classify_status(input_data: dict[str, Any]) -> str:
        explicit = input_data.get("status")
        if explicit in {"pass", "fail", "partial"}:
            return explicit
        verdict = input_data.get("review_verdict")
        exec_result = input_data.get("exec_result")
        review_approved = isinstance(verdict, dict) and bool(verdict.get("approved"))
        exec_succeeded = isinstance(exec_result, dict) and exec_result.get("exec_succeeded") is True
        if exec_succeeded and review_approved:
            return "pass"
        if exec_result is not None and not exec_succeeded:
            return "fail"
        if verdict is not None and not review_approved:
            return "fail"
        if verdict is None and exec_result is None:
            # Caller provided an explicit lesson but no automated
            # signal — partial is the honest default.
            return "partial"
        return "partial"

    @staticmethod
    def _derive_failure_keywords(input_data: dict[str, Any]) -> list[str]:
        """Quick keyword extraction from a failure signal — used when
        the caller didn't supply explicit failure_keywords."""
        kws: set[str] = set()
        verdict = input_data.get("review_verdict")
        if isinstance(verdict, dict):
            concerns = verdict.get("concerns") or []
            for c in concerns[:5]:
                if isinstance(c, str):
                    for tok in c.lower().split():
                        tok = "".join(ch for ch in tok if ch.isalnum() or ch == "_")
                        if 3 <= len(tok) <= 30:
                            kws.add(tok)
        exec_result = input_data.get("exec_result")
        if isinstance(exec_result, dict):
            stderr = (exec_result.get("stderr") or "").lower()
            for hot in (
                "assertionerror",
                "valueerror",
                "typeerror",
                "indexerror",
                "keyerror",
                "zerodivisionerror",
                "syntaxerror",
                "recursionerror",
            ):
                if hot in stderr:
                    kws.add(hot)
        return sorted(kws)[:8]

    @staticmethod
    def _build_metadata(input_data: dict[str, Any]) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        for k in ("function_name", "function_signature"):
            v = input_data.get(k)
            if v is not None:
                meta[k] = v
        verdict = input_data.get("review_verdict")
        if isinstance(verdict, dict):
            meta["code_review_approved"] = bool(verdict.get("approved"))
            meta["concerns_count"] = len(verdict.get("concerns") or [])
        exec_result = input_data.get("exec_result")
        if isinstance(exec_result, dict):
            meta["exec_succeeded"] = exec_result.get("exec_succeeded")
        return meta


__all__ = ["MemoryWriteStep", "MemoryWriteStepConfig"]
