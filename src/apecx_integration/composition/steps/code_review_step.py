"""CodeReviewStep — LLM-backed structured critique of Python code.

Pair with ``CodeWriteStep`` for the canonical generate→critique→refine
self-improvement loop (Wang et al. 2022; widely surveyed in 2026
agentic literature as the "reflection" pattern).

Output is structured: ``{approved, reasoning, concerns, suggestions,
raw_response}``. The reviewer's system prompt biases toward rejection
— false positives cost a rewrite round (cheap), false negatives cost
the user a broken artifact (expensive). Same posture as
``composition/reviewer.py:WorkflowReviewer`` (REVIEW-AGENT,
2026-05-12) applied at the step level.

Silent-failure discipline:

  1. LLM emits prose-only output (no JSON) → ``ValueError`` here.
     The wrapper does NOT default to "approved=true" on parse
     failure — a reviewer that can't be parsed is not a reviewer.
  2. LLM emits JSON with the wrong shape (missing ``approved``,
     wrong type) → ``ValueError`` here.
  3. LLM emits ``approved=false`` with empty ``concerns`` →
     ``ValueError``. A rejection without specifics is not
     actionable feedback; the wrapper rejects it so the LLM is
     forced to be concrete.

The step never SHORT-CIRCUITS to approved=true on its own. If the
operator wants a fallback path on review failure (e.g., "if review
crashes, ship anyway"), it lives in the surrounding workflow, not
in this step.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents._llm_factory import build_chat_llm

log = logging.getLogger(__name__)


_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "code_reviewer_system.md"
)


class CodeReviewStepConfig(StepConfig):
    """Configuration for CodeReviewStep.

    ``extra='forbid'`` (workspace rule).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to the reviewer system prompt. Relative paths "
            "resolved against the YAML directory. Default is "
            "``composition/code_writing_prompts/code_reviewer_system.md``."
        ),
    )

    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description=(
            "Reviewer sampling temperature. 0.0 (default) for stable "
            "verdicts — reviewer randomness creates flaky retry loops "
            "in the surrounding reflection workflow."
        ),
    )

    max_tokens: int = Field(
        default=768,
        ge=128,
        description=(
            "Max tokens for the structured verdict. 768 is enough for "
            "the JSON body + a few concerns/suggestions. Raising "
            "encourages verbose reviewers; if you see truncation on "
            "real code, bump to 1024."
        ),
    )

    require_concerns_when_rejecting: bool = Field(
        default=True,
        description=(
            "When True (default), ``approved=false`` with empty "
            "``concerns`` list is treated as an invalid verdict and "
            "raises. False allows ungrounded rejections — almost "
            "always undesirable; the flag exists for symmetry with "
            "other contract knobs."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


# Match a JSON object envelope at any position in the LLM output.
# We do NOT try to extract multiple objects — pick the first one and
# raise if it doesn't parse. Multiple-object extraction would mask
# LLM-drift.
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


class CodeReviewStep(BaseStep):
    """Review Python code against a spec and emit a structured verdict.

    Expected ``process()`` input::

        {
            "code_source": "def fib(n: int) -> int:\\n    ...",
            "code_spec": "Write a function that returns the n-th Fibonacci number.",
            "function_name": "fib",                # optional
            "function_signature": "def fib(n: int) -> int",  # optional
        }

    Return shape::

        {
            "approved": False,
            "reasoning": "Base case is wrong: f(0) returns 1, spec asks 0.",
            "concerns": ["base case f(0) returns 1, should return 0"],
            "suggestions": ["Replace f(0)=1 with f(0)=0; tests will then pass."],
            "raw_response": "{...}",
        }
    """

    COMPONENT_TYPE: str = "code_review_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return CodeReviewStepConfig

    @classmethod
    def extract_component_config(cls, config: CodeReviewStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "require_concerns_when_rejecting": (config.require_concerns_when_rejecting),
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: CodeReviewStepConfig,
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
                f"CodeReviewStep {self.name!r}: failed to read system prompt at {prompt_path}: {e}"
            ) from e
        if not self._system_prompt.strip():
            raise ValueError(
                f"CodeReviewStep {self.name!r}: system prompt at {prompt_path} is empty"
            )

        self._temperature: float = float(component_config["temperature"])
        self._max_tokens: int = int(component_config["max_tokens"])
        self._require_concerns_when_rejecting: bool = bool(
            component_config["require_concerns_when_rejecting"]
        )

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
                f"CodeReviewStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )

        if (
            "code_review_input" in input_data
            and isinstance(input_data["code_review_input"], dict)
            and "code_source" not in input_data
        ):
            input_data = input_data["code_review_input"]

        code = input_data.get("code_source")
        spec = input_data.get("code_spec")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(
                f"CodeReviewStep {self.name!r}: input_data['code_source'] "
                f"must be a non-empty string, got "
                f"{type(code).__name__}={code!r}"
            )
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(
                f"CodeReviewStep {self.name!r}: input_data['code_spec'] "
                f"must be a non-empty string, got "
                f"{type(spec).__name__}={spec!r}"
            )

        user_message = self._build_user_message(
            spec=spec,
            code=code,
            function_name=input_data.get("function_name"),
            signature=input_data.get("function_signature"),
        )

        raw = await asyncio.to_thread(self._invoke_llm, user_message=user_message)
        verdict = self._parse_verdict(raw)

        log.info(
            "CodeReviewStep %r: approved=%s, concerns=%d, suggestions=%d (raw=%d chars)",
            self.name,
            verdict["approved"],
            len(verdict["concerns"]),
            len(verdict["suggestions"]),
            len(raw),
        )

        # Passthrough fields — same convention CodeWriteStep ships
        # so a single DirectLink can wire review → memory_write
        # without an intermediate plumbing step (DirectLink is 1:1;
        # memory_write needs spec_id which originates in workflow_input).
        # ``review_verdict`` is the structured dict alias that
        # MemoryWriteStep specifically looks for.
        return {
            **verdict,
            "review_verdict": verdict,
            "code_source": code,
            "code_spec": spec,
            "function_name": input_data.get("function_name"),
            "function_signature": input_data.get("function_signature"),
            "spec_id": input_data.get("spec_id"),
            "spec_keywords": input_data.get("spec_keywords") or [],
            "attempt_n": input_data.get("attempt_n"),
        }

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
                f"CodeReviewStep {self.name!r}: LLM returned non-string "
                f"content {type(content).__name__}"
            )
        return content

    @staticmethod
    def _build_user_message(
        *,
        spec: str,
        code: str,
        function_name: str | None,
        signature: str | None,
    ) -> str:
        parts = [
            f"spec: {spec.strip()}",
            "code:\n" + code.rstrip(),
        ]
        if function_name:
            parts.append(f"function_name: {function_name}")
        if signature:
            parts.append(f"signature: {signature.strip()}")
        return "\n\n".join(parts)

    def _parse_verdict(self, raw: str) -> dict[str, Any]:
        """Parse a JSON verdict from the LLM output.

        Strategy: find the first ``{...}`` block in the response and
        parse it. Reject if the parse fails or the shape is wrong.
        Do NOT silently default to ``approved=true`` on parse failure
        — that would convert reviewer flakiness into approval drift.
        """
        match = _JSON_OBJECT_PATTERN.search(raw)
        if not match:
            raise ValueError(
                f"CodeReviewStep {self.name!r}: LLM output contains no "
                f"JSON object. Output snippet: {raw[:500]!r}"
            )
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"CodeReviewStep {self.name!r}: LLM output did not "
                f"parse as JSON ({e.msg} at pos {e.pos}). "
                f"Snippet: {match.group(0)[:300]!r}"
            ) from e

        if not isinstance(parsed, dict):
            raise ValueError(
                f"CodeReviewStep {self.name!r}: LLM JSON parsed to "
                f"{type(parsed).__name__}, expected object"
            )

        # Shape checks.
        approved = parsed.get("approved")
        if not isinstance(approved, bool):
            raise ValueError(
                f"CodeReviewStep {self.name!r}: 'approved' must be a "
                f"bool, got {type(approved).__name__}={approved!r}"
            )

        reasoning = parsed.get("reasoning", "")
        if not isinstance(reasoning, str):
            raise ValueError(
                f"CodeReviewStep {self.name!r}: 'reasoning' must be a "
                f"string, got {type(reasoning).__name__}"
            )

        concerns_raw = parsed.get("concerns", [])
        if not isinstance(concerns_raw, list):
            raise ValueError(
                f"CodeReviewStep {self.name!r}: 'concerns' must be a "
                f"list, got {type(concerns_raw).__name__}"
            )
        concerns = [str(c) for c in concerns_raw]

        suggestions_raw = parsed.get("suggestions", [])
        if not isinstance(suggestions_raw, list):
            raise ValueError(
                f"CodeReviewStep {self.name!r}: 'suggestions' must be a "
                f"list, got {type(suggestions_raw).__name__}"
            )
        suggestions = [str(s) for s in suggestions_raw]

        # Grounded-rejection gate: approved=false WITHOUT concerns is
        # not actionable feedback. Reject so the LLM is forced to be
        # specific or the operator can disable this knob explicitly.
        if not approved and self._require_concerns_when_rejecting and not concerns:
            raise ValueError(
                f"CodeReviewStep {self.name!r}: LLM emitted "
                f"approved=false with no concerns. Set "
                f"require_concerns_when_rejecting: false to allow "
                f"this, or fix the system prompt to demand concerns."
            )

        return {
            "approved": approved,
            "reasoning": reasoning,
            "concerns": concerns,
            "suggestions": suggestions,
            "raw_response": raw,
        }


__all__ = ["CodeReviewStep", "CodeReviewStepConfig"]
