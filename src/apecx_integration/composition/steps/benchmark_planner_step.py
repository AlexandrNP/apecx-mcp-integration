"""BenchmarkPlannerStep — plan-then-code planner stage.

Companion to ``BenchmarkDrafterStep``. The planner reads a benchmark
problem prompt, emits a SHORT numbered plan (3–6 steps), and packages
the plan into an enriched ``code_spec`` that the downstream drafter
step consumes verbatim. The drafter does NOT have to know about
planning — it just reads ``code_spec`` like any direct-codegen call.

Why a separate step from ``BenchmarkDrafterStep``
-------------------------------------------------

* Different system prompt (planner: emit plan, no code; drafter: emit
  code, no prose).
* Different output extraction (planner: strip ``<think>...</think>``,
  take everything else as plan text; drafter: pull the largest
  ```python fenced block).
* Different output shape (planner: ``{code_spec: <enriched>, plan,
  entry_point, test_hint}``; drafter: ``{code_source: str}``).
* Different model role (planner → ``nemotron-3-nano:4b`` by default,
  drafter → ``mistral-nemo:latest``).

Mixing both into one parameterized step would force runtime branches
that obscure the contract. Two thin classes beat one polymorphic class.

I/O contract
------------

Input ``process(input_data)``::

    {"code_spec": str, "entry_point"?: str, "test_hint"?: str,
     "function_signature"?: str}

Output::

    {"code_spec": "<original prompt>\n\nSuggested plan:\n<plan>",
     "plan": "<plan text>",
     "entry_point": str | None,
     "test_hint": str | None,
     "function_signature": str | None}

The enriched ``code_spec`` is consumed by the drafter step's
``code_spec`` input field via a DirectLink with ``auto_transfer: true``.
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
_FENCE_PATTERN = re.compile(r"```(?:python|py)?\s*\n.*?\n\s*```", re.DOTALL)

_KNOWN_ROLES: frozenset[str] = frozenset({"drafter", "planner", "reviewer"})


class BenchmarkPlannerStepConfig(StepConfig):
    """Configuration for ``BenchmarkPlannerStep``.

    ``extra='forbid'`` (workspace rule): YAML typos fail loud at load.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to the planner system prompt. Default bundled prompt "
            "at ``composition/code_writing_prompts/benchmark_planner_system.md``."
        ),
    )

    role: str = Field(
        default="planner",
        description="Which model_roles entry to use. Default: planner.",
    )

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64)

    request_timeout_seconds: float = Field(
        default=45.0,
        ge=1.0,
        description=(
            "Hard wall-clock cap on the planner's LLM HTTP call. Tighter "
            "default than the drafter (45s vs 60s) because the planner is "
            "where nemotron-3-nano:4b's thinking-token blow-up bites: a "
            "single problem can hang for 5+ minutes producing endless "
            "``<think>...</think>`` blocks. The framework's "
            "Workflow.wait_for_cascade is a settle-quiet probe, not a "
            "request budget, so this LLM-level cap is the last line of "
            "defense. 45s = enough for a sane 3-6 step plan; longer => "
            "the model is thinking-looping and the result is unusable."
        ),
    )

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
                f"BenchmarkPlannerStepConfig: role={self.role!r} is not "
                f"one of {sorted(_KNOWN_ROLES)}."
            )
        return self


_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "benchmark_planner_system.md"
)


class BenchmarkPlannerStep(BaseStep):
    """Planner stage of the plan-then-code scaffold."""

    COMPONENT_TYPE: str = "benchmark_planner_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return BenchmarkPlannerStepConfig

    @classmethod
    def extract_component_config(cls, config: BenchmarkPlannerStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "role": config.role,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "request_timeout_seconds": config.request_timeout_seconds,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: BenchmarkPlannerStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)

        prompt_path = self._resolve_prompt_path(
            component_config.get("system_prompt_file"),
            component_config.get("source_path"),
        )
        try:
            self._system_prompt: str = prompt_path.read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(
                f"BenchmarkPlannerStep {self.name!r}: failed to read system "
                f"prompt at {prompt_path}: {e}"
            ) from e
        if not self._system_prompt.strip():
            raise ValueError(
                f"BenchmarkPlannerStep {self.name!r}: system prompt at {prompt_path} is empty"
            )

        self._role: str = component_config["role"]
        self._temperature: float = float(component_config["temperature"])
        self._max_tokens: int = int(component_config["max_tokens"])
        self._request_timeout_seconds: float = float(component_config["request_timeout_seconds"])

    @staticmethod
    def _resolve_prompt_path(configured: str | None, source_path: str | None) -> Path:
        if configured is None:
            return _DEFAULT_PROMPT_PATH
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
                f"BenchmarkPlannerStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )

        if (
            len(input_data) == 1
            and "code_spec" not in input_data
            and "problem_prompt" not in input_data
        ):
            (only_key,) = input_data.keys()
            if isinstance(input_data[only_key], dict):
                input_data = input_data[only_key]

        spec = input_data.get("code_spec") or input_data.get("problem_prompt")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(
                f"BenchmarkPlannerStep {self.name!r}: input_data['code_spec'] "
                f"must be a non-empty string"
            )

        user_message = self._build_user_message(
            spec=spec,
            entry_point=input_data.get("entry_point"),
            test_hint=input_data.get("test_hint"),
            function_signature=input_data.get("function_signature"),
        )

        model, base_url = _resolve_role_model(self._role)
        raw = await asyncio.to_thread(
            self._invoke_llm,
            user_message=user_message,
            model=model,
            base_url=base_url,
        )

        plan = self._extract_plan(raw)
        if not plan.strip():
            raise ValueError(
                f"BenchmarkPlannerStep {self.name!r}: LLM returned empty plan (model={model!r})"
            )

        enriched_spec = f"{spec.strip()}\n\nSuggested plan:\n{plan.strip()}"

        log.info(
            "BenchmarkPlannerStep %r: %d-char plan (role=%r, model=%r)",
            self.name,
            len(plan),
            self._role,
            model,
        )

        return {
            "code_spec": enriched_spec,
            "plan": plan,
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
            raise ValueError(f"BenchmarkPlannerStep {self.name!r}: non-string LLM content")
        return content

    @staticmethod
    def _build_user_message(
        *,
        spec: str,
        entry_point: str | None,
        test_hint: str | None,
        function_signature: str | None,
    ) -> str:
        parts = [spec.strip()]
        if entry_point:
            parts.append(f"Target function: ``{entry_point}``")
        if function_signature:
            parts.append(f"Function signature: {function_signature.strip()}")
        if test_hint:
            parts.append(f"Test contract:\n{test_hint.strip()}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_plan(raw: str) -> str:
        """Strip think blocks + any code fences (the drafter writes code,
        not the planner; if the planner emits code it's noise)."""
        cleaned = _THINK_BLOCK.sub("", raw)
        # Remove any fenced blocks — planner output should be text only.
        cleaned = _FENCE_PATTERN.sub("", cleaned)
        return cleaned.strip()


__all__ = ["BenchmarkPlannerStep", "BenchmarkPlannerStepConfig"]
