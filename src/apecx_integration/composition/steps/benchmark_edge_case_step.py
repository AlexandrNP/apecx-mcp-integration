"""BenchmarkEdgeCaseStep — pre-drafter agent that enumerates edge cases.

MB-1 from ``composer_scaffold_designs_per_benchmark.md``. The MBPP
failure analysis (F1, F8): 11/50 fail_assertion problems under
plan-then-code v2 are logic bugs where the model produces code that
handles the happy path but misses edge cases (empty input, off-by-
one, negative numbers, type coercion). An agent that ENUMERATES
those edge cases BEFORE the drafter writes code lets the drafter
defend against them.

I/O contract
------------

Input ``process(input_data)``::

    {"code_spec": str, "entry_point"?: str, "test_hint"?: str,
     "function_signature"?: str}

Output::

    {"code_spec": "<original>\\n\\nEdge cases to handle:\\n<bullets>",
     "edge_cases": "<bullet list>",
     "entry_point", "test_hint", "function_signature": passthrough}

The downstream drafter reads ``code_spec`` (now enriched) and
writes code with the edge cases in context. The drafter doesn't
need new fields; the enrichment is in-place.

Why not a separate ``edge_cases`` field
---------------------------------------

Two-stage scaffolds in this workspace use ``code_spec`` enrichment
(matches ``BenchmarkPlannerStep``'s "Suggested plan:" prepend
pattern). A separate field would require modifying every downstream
drafter's user_message construction. Enrichment is cheaper and
keeps the drafter agnostic.

Silent-failure discipline
-------------------------

* Empty ``code_spec`` → ``ValueError``.
* Empty LLM response → ``ValueError`` (no silent passthrough).
* Output enriches the spec deterministically; drafter sees the
  combined prompt; failure-to-honor-edge-cases is a model-quality
  signal, not a silent failure.
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


class BenchmarkEdgeCaseStepConfig(StepConfig):
    """Configuration for ``BenchmarkEdgeCaseStep``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to edge-case system prompt. Default: "
            "``composition/code_writing_prompts/benchmark_edge_case_system.md``."
        ),
    )
    role: str = Field(
        default="planner",
        description=(
            "Which model_roles entry. Default 'planner' -- the planner "
            "model is the right size for this lightweight enumeration "
            "(nemotron-3-nano:4b by default)."
        ),
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=64)
    request_timeout_seconds: float = Field(default=45.0, ge=1.0)

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
                f"BenchmarkEdgeCaseStepConfig: role={self.role!r} is not one "
                f"of {sorted(_KNOWN_ROLES)}."
            )
        return self


_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "code_writing_prompts"
    / "benchmark_edge_case_system.md"
)


class BenchmarkEdgeCaseStep(BaseStep):
    """Edge-case enumerator stage for MBPP-style problems."""

    COMPONENT_TYPE: str = "benchmark_edge_case_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return BenchmarkEdgeCaseStepConfig

    @classmethod
    def extract_component_config(cls, config: BenchmarkEdgeCaseStepConfig) -> dict[str, Any]:
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
        config: BenchmarkEdgeCaseStepConfig,
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
                f"BenchmarkEdgeCaseStep {self.name!r}: failed to read system "
                f"prompt at {prompt_path}: {e}"
            ) from e
        if not self._system_prompt.strip():
            raise ValueError(f"BenchmarkEdgeCaseStep {self.name!r}: empty system prompt")

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
            raise ValueError(f"BenchmarkEdgeCaseStep {self.name!r}: input_data must be a dict")

        # Trigger-envelope unwrap.
        if len(input_data) == 1 and "code_spec" not in input_data:
            (only_key,) = input_data.keys()
            if isinstance(input_data[only_key], dict):
                input_data = input_data[only_key]

        spec = input_data.get("code_spec") or input_data.get("problem_prompt")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(f"BenchmarkEdgeCaseStep {self.name!r}: empty code_spec")

        user_message = (
            f"Problem:\n{spec.strip()}\n\n"
            "Enumerate 2-5 edge cases the solution should handle. "
            "Format each as a single line starting with `- `. No code, "
            "no explanation -- just the edge case names."
        )

        model, base_url = _resolve_role_model(self._role)
        raw = await asyncio.to_thread(
            self._invoke_llm,
            user_message=user_message,
            model=model,
            base_url=base_url,
        )

        cleaned = _THINK_BLOCK.sub("", raw).strip()
        if not cleaned:
            raise ValueError(
                f"BenchmarkEdgeCaseStep {self.name!r}: LLM returned empty edge-case list"
            )

        # Filter to bullet lines only — if the model added prose, drop it.
        edge_lines = [line.strip() for line in cleaned.splitlines() if line.strip().startswith("-")]
        if not edge_lines:
            # No bullets — treat the whole output as one bullet.
            edge_lines = [f"- {cleaned.strip().splitlines()[0]}"]
        edge_cases = "\n".join(edge_lines)

        enriched_spec = f"{spec.strip()}\n\nEdge cases to handle:\n{edge_cases}"

        log.info(
            "BenchmarkEdgeCaseStep %r: %d edge cases (role=%r, model=%r)",
            self.name,
            len(edge_lines),
            self._role,
            model,
        )

        return {
            "code_spec": enriched_spec,
            "edge_cases": edge_cases,
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
            raise ValueError(f"BenchmarkEdgeCaseStep {self.name!r}: non-string LLM content")
        return content


__all__ = ["BenchmarkEdgeCaseStep", "BenchmarkEdgeCaseStepConfig"]
