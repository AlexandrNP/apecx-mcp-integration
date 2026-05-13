"""BenchmarkReviewerStep — middle stage of the review-revise scaffold.

Reads a code spec + a candidate solution, produces a critique. The
critique drives a subsequent reviser step that revises the candidate.

Why a separate class (not bending BenchmarkDrafterStep)
-------------------------------------------------------

The reviewer's task is fundamentally different from a drafter's:

* Drafter: spec → code
* Reviewer: spec + candidate code → critique (text)

A separate class makes the contract explicit and keeps the system
prompt + output extraction focused. Mixing both into a polymorphic
``mode: draft|review`` step would force runtime branches that obscure
the contract — same reasoning as ``BenchmarkPlannerStep``.

I/O contract
------------

Input ``process(input_data)`` (after framework-trigger-unwrap)::

    {"code_spec": str, "code_source": str,
     "entry_point"?: str, "test_hint"?: str,
     "function_signature"?: str}

Output::

    {"code_spec": <passthrough>,
     "previous_attempt": <was input_data['code_source']>,
     "critique": "<reviewer text>",
     "entry_point", "test_hint", "function_signature": <passthrough>}

The output schema is what the downstream reviser (a
``BenchmarkDrafterStep`` with a different role/prompt) reads. The
reviser then uses ``previous_attempt`` + ``critique`` to produce
revised code.

Silent-failure discipline
-------------------------

* Empty ``code_spec`` → ``ValueError`` (mirrors drafter).
* Empty ``code_source`` → ``ValueError`` (we can't review nothing).
* Empty LLM response → ``ValueError`` (no silent passthrough).
* Reviewer-emitted code is acceptable in the critique text (it's
  text-shaped advice; the downstream reviser is free to use or
  ignore embedded code).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents._llm_factory import build_chat_llm
from apecx_integration.composition.steps.benchmark_drafter_step import (
    _resolve_role_model,
)

log = logging.getLogger(__name__)

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

_KNOWN_ROLES: frozenset[str] = frozenset({"drafter", "planner", "reviewer"})


class BenchmarkReviewerStepConfig(StepConfig):
    """Configuration for ``BenchmarkReviewerStep``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to reviewer system prompt. Default: "
            "``composition/code_writing_prompts/benchmark_reviewer_system.md``."
        ),
    )
    rules_file: str | None = Field(
        default=None,
        description="Optional rules file appended to system prompt (same shape as drafter).",
    )
    role: str = Field(
        default="reviewer",
        description="Which model_roles entry to use. Default: reviewer.",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64)
    request_timeout_seconds: float = Field(default=60.0, ge=1.0)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    @model_validator(mode="after")
    def _validate_role(self):
        if self.role not in _KNOWN_ROLES:
            raise ValueError(
                f"BenchmarkReviewerStepConfig: role={self.role!r} is not "
                f"one of {sorted(_KNOWN_ROLES)}."
            )
        return self


_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "benchmark_reviewer_system.md"
)


class BenchmarkReviewerStep(BaseStep):
    """Reviewer stage of the review-revise scaffold."""

    COMPONENT_TYPE: str = "benchmark_reviewer_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return BenchmarkReviewerStepConfig

    @classmethod
    def extract_component_config(cls, config: BenchmarkReviewerStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "rules_file": config.rules_file,
            "role": config.role,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "request_timeout_seconds": config.request_timeout_seconds,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: BenchmarkReviewerStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)

        prompt_path = self._resolve_path(
            component_config.get("system_prompt_file"),
            component_config.get("source_path"),
            default=_DEFAULT_PROMPT_PATH,
        )
        try:
            self._system_prompt: str = prompt_path.read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(
                f"BenchmarkReviewerStep {self.name!r}: failed to read system "
                f"prompt at {prompt_path}: {e}"
            ) from e
        if not self._system_prompt.strip():
            raise ValueError(
                f"BenchmarkReviewerStep {self.name!r}: empty system prompt at {prompt_path}"
            )

        rules_path = self._resolve_path(
            component_config.get("rules_file"),
            component_config.get("source_path"),
            default=None,
        )
        if rules_path is not None:
            try:
                rules_text = rules_path.read_text(encoding="utf-8")
            except OSError as e:
                raise ValueError(
                    f"BenchmarkReviewerStep {self.name!r}: failed to read rules file: {e}"
                ) from e
            if rules_text.strip():
                self._system_prompt = (
                    self._system_prompt.rstrip() + "\n\n---\n\n" + rules_text.strip() + "\n"
                )

        self._role: str = component_config["role"]
        self._temperature: float = float(component_config["temperature"])
        self._max_tokens: int = int(component_config["max_tokens"])
        self._request_timeout_seconds: float = float(component_config["request_timeout_seconds"])

    @staticmethod
    def _resolve_path(
        configured: str | None,
        source_path: str | None,
        *,
        default: Path | None,
    ) -> Path | None:
        if configured is None:
            return default
        p = Path(configured)
        if p.is_absolute():
            return p
        if source_path:
            return (Path(source_path).resolve().parent / p).resolve()
        return (Path.cwd() / p).resolve()

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"BenchmarkReviewerStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )

        # Trigger-envelope unwrap (mirrors drafter logic).
        if (
            len(input_data) == 1
            and "code_spec" not in input_data
            and "code_source" not in input_data
        ):
            (only_key,) = input_data.keys()
            if isinstance(input_data[only_key], dict):
                input_data = input_data[only_key]

        spec = input_data.get("code_spec")
        candidate = input_data.get("code_source")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(
                f"BenchmarkReviewerStep {self.name!r}: input_data['code_spec'] "
                f"must be a non-empty string"
            )
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError(
                f"BenchmarkReviewerStep {self.name!r}: input_data['code_source'] "
                f"must be a non-empty string (cannot review nothing)"
            )

        user_message = (
            f"Specification:\n{spec.strip()}\n\n"
            f"Candidate solution:\n```python\n{candidate.strip()}\n```\n\n"
            "Write a concise critique. Focus on correctness, framework "
            "conventions, and the spec's requirements. If the candidate "
            "is correct, reply with exactly the single word `PASS`."
        )

        model, base_url = _resolve_role_model(self._role)
        raw = await asyncio.to_thread(
            self._invoke_llm,
            user_message=user_message,
            model=model,
            base_url=base_url,
        )

        critique = _THINK_BLOCK.sub("", raw).strip()
        if not critique:
            raise ValueError(
                f"BenchmarkReviewerStep {self.name!r}: LLM returned empty "
                f"critique (model={model!r})."
            )

        log.info(
            "BenchmarkReviewerStep %r: %d-char critique (role=%r, model=%r, pass=%s)",
            self.name,
            len(critique),
            self._role,
            model,
            critique.strip().upper() == "PASS",
        )

        return {
            "code_spec": spec,
            "previous_attempt": candidate,
            "critique": critique,
            "entry_point": input_data.get("entry_point"),
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
        }

    def _invoke_llm(self, *, user_message: str, model: str, base_url: str) -> str:
        llm = build_chat_llm(
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            model=model,
            base_url=base_url,
            request_timeout=self._request_timeout_seconds,
        )
        from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

        response = llm.invoke(
            [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_message),
            ]
        )
        content = getattr(response, "content", "")
        if not isinstance(content, str):
            raise ValueError(f"BenchmarkReviewerStep {self.name!r}: non-string LLM content")
        return content


__all__ = ["BenchmarkReviewerStep", "BenchmarkReviewerStepConfig"]
