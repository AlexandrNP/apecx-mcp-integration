"""MultiSampleDrafterStep — fan-out drafter for structural consensus (SGDe-style).

Implements the "structural consensus" pattern from
``2604.17450v3.pdf`` (SGDe). Where a single drafter call at
temperature=0 produces deterministic-but-possibly-wrong output,
this step samples N candidates at temperature > 0 and emits the
full list. A downstream deterministic aggregator picks the winner.

Why fan-out helps SLMs
----------------------

F14 finding: at temperature=0, mistral-nemo 12B produces identical
revised code regardless of critique source. The model has no
variance to vote across — every sample is the same.

F17 finding: at temperature=0 with worked examples, mistral-nemo
imitates the example reliably (80% on nanobrain-native step
problems) but fails deterministically on the 3 hard problems.

Structural-consensus hypothesis (this step): at temperature > 0,
the 3 hard problems may produce SOME correct candidates AND some
wrong ones. A deterministic voter (validator) picks the correct
one. SGDe reports +26-34pp on GSM-Hard from this alone.

This step is the LLM half of structural consensus. The aggregator
(``ConsensusAggregatorStep``) is the deterministic-voter half.

I/O contract
------------

Input: same as ``BenchmarkDrafterStep`` (``code_spec``, optional
``entry_point`` etc.).

Output::

    {"candidates": [
        {"code_source": str},
        {"code_source": str},
        ...
     ],
     "n_samples": int,
     "temperature": float,
     "model": str,
     "code_spec", "entry_point", "test_hint", "function_signature": passthrough}

Silent-failure discipline
-------------------------

* Empty ``code_spec`` → ``ValueError``.
* All N samples return empty → ``ValueError`` (no silent passthrough).
* Some samples return empty → those are dropped; the remaining list
  is emitted. Aggregator handles "fewer than n_samples" gracefully.
* N=0 → ``ValueError`` at config-load.
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


_FENCE_PATTERN = re.compile(r"```(?:python|py)?\s*\n(.*?)\n\s*```", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

_KNOWN_ROLES: frozenset[str] = frozenset({"drafter", "planner", "reviewer"})


class MultiSampleDrafterStepConfig(StepConfig):
    """Configuration for ``MultiSampleDrafterStep``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    system_prompt_file: str | None = Field(default=None)
    rules_file: str | None = Field(default=None)
    role: str = Field(default="drafter")

    n_samples: int = Field(
        default=3,
        ge=1,
        description=(
            "Number of independent samples to draw from the drafter. "
            "SGDe uses N=3 as default. Larger N -> higher chance of "
            "covering the correct answer but linear LLM cost growth."
        ),
    )
    temperature: float = Field(
        default=0.5,
        ge=0.0,
        le=2.0,
        description=(
            "Sampling temperature for variance. 0.0 gives identical "
            "samples (defeats the purpose). 0.5 is a starting point "
            "for mistral-nemo; tune based on per-problem variance."
        ),
    )
    max_tokens: int = Field(default=1024, ge=64)
    request_timeout_seconds: float = Field(default=60.0, ge=1.0)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    @model_validator(mode="after")
    def _validate_role_and_temp(self):
        if self.role not in _KNOWN_ROLES:
            raise ValueError(
                f"MultiSampleDrafterStepConfig: role={self.role!r} is not "
                f"one of {sorted(_KNOWN_ROLES)}."
            )
        if self.temperature == 0.0 and self.n_samples > 1:
            # FAIL-FAST silent-failure guard: temp=0 + N>1 gives N
            # identical samples. The user almost certainly meant
            # temp > 0 for multi-sample.
            raise ValueError(
                f"MultiSampleDrafterStepConfig: n_samples={self.n_samples} "
                f"with temperature=0.0 produces identical samples. Set "
                f"temperature > 0 or n_samples=1."
            )
        return self


_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "benchmark_drafter_system.md"
)


class MultiSampleDrafterStep(BaseStep):
    """Fan-out drafter producing N candidates at temperature > 0."""

    COMPONENT_TYPE: str = "multi_sample_drafter_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return MultiSampleDrafterStepConfig

    @classmethod
    def extract_component_config(cls, config: MultiSampleDrafterStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "rules_file": config.rules_file,
            "role": config.role,
            "n_samples": config.n_samples,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "request_timeout_seconds": config.request_timeout_seconds,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: MultiSampleDrafterStepConfig,
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
                f"MultiSampleDrafterStep {self.name!r}: failed to read prompt at {prompt_path}: {e}"
            ) from e

        # Optional rules append.
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
                    f"MultiSampleDrafterStep {self.name!r}: failed to read "
                    f"rules at {rules_path}: {e}"
                ) from e
            if rules_text.strip():
                self._system_prompt = (
                    self._system_prompt.rstrip() + "\n\n---\n\n" + rules_text.strip() + "\n"
                )

        self._role: str = component_config["role"]
        self._n_samples: int = int(component_config["n_samples"])
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
            raise ValueError(f"MultiSampleDrafterStep {self.name!r}: input_data must be a dict")

        # Trigger-envelope unwrap.
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
            raise ValueError(f"MultiSampleDrafterStep {self.name!r}: empty code_spec")

        user_message = self._build_user_message(
            spec=spec,
            entry_point=input_data.get("entry_point"),
            test_hint=input_data.get("test_hint"),
            function_signature=input_data.get("function_signature"),
        )

        model, base_url = _resolve_role_model(self._role)

        # Fan out N parallel samples via asyncio.gather. Each call is
        # an independent LLM round-trip; the LLM seeds via temperature
        # so the samples diverge.
        async def _one_sample() -> str:
            raw = await asyncio.to_thread(
                self._invoke_llm,
                user_message=user_message,
                model=model,
                base_url=base_url,
            )
            return self._extract_code(raw)

        candidates_raw: list[str] = await asyncio.gather(
            *[_one_sample() for _ in range(self._n_samples)]
        )

        # Drop empty samples; the aggregator handles a partial list.
        candidates = [{"code_source": c} for c in candidates_raw if c.strip()]

        if not candidates:
            raise ValueError(
                f"MultiSampleDrafterStep {self.name!r}: all {self._n_samples} "
                f"samples returned empty (model={model!r}, temp={self._temperature})"
            )

        log.info(
            "MultiSampleDrafterStep %r: %d/%d non-empty samples (role=%r, model=%r, T=%s)",
            self.name,
            len(candidates),
            self._n_samples,
            self._role,
            model,
            self._temperature,
        )

        return {
            "candidates": candidates,
            "n_samples": self._n_samples,
            "temperature": self._temperature,
            "model": model,
            "code_spec": spec,
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
            return ""
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
        if function_signature:
            parts.append(f"Function signature:\n{function_signature.strip()}")
        if entry_point:
            parts.append(f"Define a function named ``{entry_point}``.")
        if test_hint:
            parts.append(f"Your code must satisfy:\n{test_hint.strip()}")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_code(raw: str) -> str:
        cleaned = _THINK_BLOCK.sub("", raw)
        candidates = _FENCE_PATTERN.findall(cleaned)
        if candidates:
            return max(candidates, key=len)
        return cleaned.strip()


__all__ = ["MultiSampleDrafterStep", "MultiSampleDrafterStepConfig"]
