"""WorkflowSummarizerStep — LLM-backed plain-English explainer.

Consumes the structured dict produced by ``WorkflowAnalysisStep`` and
emits a Markdown body explaining the workflow to a domain expert.
Grounded: the LLM sees ONLY the analysis dict, not raw YAML, so it
can't hallucinate structural claims (the analysis already validated
shape).

Pair with ``WorkflowAnalysisStep`` for the canonical "explain this
generated workflow" flow:

    WorkflowAnalysisStep → analysis dict → WorkflowSummarizerStep → Markdown

The two are intentionally split: analysis is deterministic + cheap;
summarization is LLM-bound + variable. Operators can rerun the
summarizer with different models / temperatures to compare
explanations without re-paying the analysis cost.

Silent-failure discipline:

  1. Empty / wrong-shape analysis dict → ``ValueError``.
  2. Empty LLM response → ``ValueError`` (EMPTY-FAIL).
  3. Response that drops the required Markdown headings ("What this
     workflow does", "Steps", "Data flow", "Issues to know about",
     "Honest caveats") → ``ValueError``. Pinned because a summary
     missing sections is worse than no summary — adopters trust
     section headings.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents._llm_factory import build_chat_llm

log = logging.getLogger(__name__)

_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "code_writing_prompts"
    / "workflow_summarizer_system.md"
)

_REQUIRED_SECTIONS = (
    "## What this workflow does",
    "## Steps",
    "## Data flow",
    "## Issues to know about",
    "## Honest caveats",
)


class WorkflowSummarizerStepConfig(StepConfig):
    """Configuration for WorkflowSummarizerStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)
    system_prompt_file: str | None = Field(default=None)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=128)
    require_all_sections: bool = Field(
        default=True,
        description=(
            "When True (default), the response must contain ALL "
            "required Markdown section headings or raise. Operators "
            "running this on a small/weak model may opt out, but the "
            "trade-off is adopter trust — a summary missing 'Honest "
            "caveats' might omit critical limitations."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class WorkflowSummarizerStep(BaseStep):
    """Explain a workflow in plain English to a domain expert.

    Expected ``process()`` input::

        {"analysis": <dict produced by WorkflowAnalysisStep>}

    Return shape::

        {"summary_markdown": "## What this workflow does\\n...", "raw_response": "..."}
    """

    COMPONENT_TYPE: str = "workflow_summarizer_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return WorkflowSummarizerStepConfig

    @classmethod
    def extract_component_config(cls, config: WorkflowSummarizerStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "require_all_sections": config.require_all_sections,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: WorkflowSummarizerStepConfig,
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
                f"WorkflowSummarizerStep {self.name!r}: failed to read "
                f"system prompt at {prompt_path}: {e}"
            ) from e
        if not self._system_prompt.strip():
            raise ValueError(
                f"WorkflowSummarizerStep {self.name!r}: system prompt at {prompt_path} is empty"
            )
        self._temperature: float = float(component_config["temperature"])
        self._max_tokens: int = int(component_config["max_tokens"])
        self._require_all_sections: bool = bool(component_config["require_all_sections"])

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
                f"WorkflowSummarizerStep {self.name!r}: input_data must "
                f"be a dict, got {type(input_data).__name__}"
            )
        if (
            "summarizer_input" in input_data
            and isinstance(input_data["summarizer_input"], dict)
            and "analysis" not in input_data
        ):
            input_data = input_data["summarizer_input"]

        analysis = input_data.get("analysis")
        if not isinstance(analysis, dict):
            raise ValueError(
                f"WorkflowSummarizerStep {self.name!r}: input_data must "
                f"contain 'analysis' (dict produced by "
                f"WorkflowAnalysisStep); got "
                f"{type(analysis).__name__}"
            )
        for required_key in ("workflow_name", "steps", "links", "issues"):
            if required_key not in analysis:
                raise ValueError(
                    f"WorkflowSummarizerStep {self.name!r}: analysis "
                    f"dict missing required key {required_key!r}; got "
                    f"keys={sorted(analysis.keys())}. The analysis must "
                    f"be produced by WorkflowAnalysisStep (or a "
                    f"compatible producer)."
                )

        user_message = self._build_user_message(analysis)
        raw = await asyncio.to_thread(self._invoke_llm, user_message=user_message)
        if not raw.strip():
            raise ValueError(
                f"WorkflowSummarizerStep {self.name!r}: LLM produced "
                f"empty response — EMPTY-FAIL discipline."
            )

        body = raw.strip()
        if self._require_all_sections:
            missing = [s for s in _REQUIRED_SECTIONS if s not in body]
            if missing:
                raise ValueError(
                    f"WorkflowSummarizerStep {self.name!r}: LLM "
                    f"response missing required sections {missing}. "
                    f"Set require_all_sections=False to tolerate "
                    f"weaker models that drop sections; default is "
                    f"True because missing 'Honest caveats' is a real "
                    f"adopter-trust regression."
                )

        log.info(
            "WorkflowSummarizerStep %r: produced %d-char summary",
            self.name,
            len(body),
        )
        return {"summary_markdown": body, "raw_response": raw}

    def _invoke_llm(self, *, user_message: str) -> str:
        llm = build_chat_llm(temperature=self._temperature, max_tokens=self._max_tokens)
        from langchain_core.messages import HumanMessage, SystemMessage

        response = llm.invoke(
            [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_message),
            ]
        )
        content = getattr(response, "content", "")
        if not isinstance(content, str):
            raise ValueError(
                f"WorkflowSummarizerStep {self.name!r}: LLM returned "
                f"non-string content {type(content).__name__}"
            )
        return content

    @staticmethod
    def _build_user_message(analysis: dict[str, Any]) -> str:
        import json

        return "Structured workflow analysis (parse this carefully):\n\n" + json.dumps(
            analysis, indent=2, default=str
        )


__all__ = ["WorkflowSummarizerStep", "WorkflowSummarizerStepConfig"]
