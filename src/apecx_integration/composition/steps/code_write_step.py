"""CodeWriteStep — LLM-backed Python code authoring with strict gates.

Generates a single Python function (or small module) from a
natural-language spec. The step is the first leg of any code-writing
workflow: it shapes the LLM call, parses + validates the output, and
raises on any failure shape that would otherwise propagate silently
downstream.

Silent-failure discipline (per the workspace CLAUDE.md + the
EMPTY-FAIL gate hardening from 2026-05-12):

  1. The LLM returns prose-only output (a refusal, an explanation
     without code) → ``ast.parse`` fails → ``ValueError`` here, not
     a silent empty-string passthrough.
  2. The LLM returns code that doesn't define the requested function
     → AST inspection detects the absence → ``ValueError`` here,
     not a downstream KeyError 30s later.
  3. The LLM wraps output in ``` ```python ... ``` ``` despite the
     system prompt → we strip the fences once and re-parse; if it
     still doesn't parse, raise (do NOT try multiple parsing
     strategies — that masks LLM drift).
  4. Empty input ``code_spec`` → ``ValueError`` before any LLM call
     (matches the executor's EMPTY-FAIL gate at the step layer).

Framework-native packaging:
  - Subclasses ``BaseStep``; ``async def process``; never overrides
    ``execute()`` (per nanobrain Method Responsibility Matrix).
  - Config extends ``StepConfig``; ``extra='forbid'`` so YAML typos
    fail at load time (workspace rule).
  - System prompt loaded from a path-resolved file at step init —
    no hardcoded prompts (per nanobrain rule #4).
  - LLM client built via ``apecx_integration.agents._llm_factory.build_chat_llm``
    so APECX_LLM_* env vars work without code changes.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents._llm_factory import build_chat_llm

log = logging.getLogger(__name__)


# Default prompt path — packaged alongside the step. Relative paths in
# the wrapper YAML's ``system_prompt_file`` field are resolved against
# the YAML directory; this absolute fallback is used only when no
# path is configured.
_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "code_writer_system.md"
)


# Patterns the LLM emits in violation of the system prompt; we strip
# them defensively then re-parse. If still unparseable, we raise —
# stripping multiple times would mask a real LLM-drift issue.
_FENCE_OPEN_PATTERN = re.compile(r"^\s*```(?:python|py)?\s*\n", re.MULTILINE)
_FENCE_CLOSE_PATTERN = re.compile(r"\n```\s*$", re.MULTILINE)


class CodeWriteStepConfig(StepConfig):
    """Configuration for CodeWriteStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise at config
    load rather than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Framework tracking attribute populated by ConfigBase.from_config —
    # declared so extra='forbid' doesn't reject it.
    source_path: str | None = Field(default=None)

    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to the system prompt markdown file. Relative paths "
            "are resolved against this YAML's directory. When None "
            "the bundled default at "
            "``composition/code_writing_prompts/code_writer_system.md`` "
            "is used."
        ),
    )

    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description=(
            "Sampling temperature. 0.0 (default) for deterministic code "
            "output — code generation is one of the tasks where "
            "exploration hurts more than it helps. Operators can "
            "override via APECX_LLM_TEMPERATURE without editing this."
        ),
    )

    max_tokens: int = Field(
        default=1024,
        ge=64,
        description=(
            "Max tokens for the response. 1024 is enough for most "
            "single-function asks; raise for multi-function modules. "
            "APECX_LLM_MAX_TOKENS env var overrides."
        ),
    )

    require_function_name: bool = Field(
        default=True,
        description=(
            "When True (default), the AST gate verifies that "
            "``input_data['function_name']`` (or "
            "``default_function_name`` below) is defined at module "
            "scope. False disables the check — useful for free-form "
            "code-writing where the LLM picks the name."
        ),
    )

    default_function_name: str | None = Field(
        default=None,
        description=(
            "Function name to verify when input_data['function_name'] "
            "is not supplied. Either supply per-call or set here for "
            "composed workflows where the function name is fixed by "
            "the surrounding workflow."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class CodeWriteStep(BaseStep):
    """Generate Python code from a natural-language spec.

    Expected ``process()`` input::

        {
            "code_spec": "Write a function that returns the n-th Fibonacci number, base cases f(0)=0, f(1)=1.",
            "function_name": "fib",            # optional, falls back to default_function_name
            "function_signature": "def fib(n: int) -> int",  # optional
            "previous_attempt": "<code from prior round>",   # optional, refinement only
            "critique": "<reviewer concerns>",                # optional, refinement only
        }

    Return shape::

        {
            "code_source": "def fib(n: int) -> int:\\n    ...\\n",
            "function_name_verified": "fib",  # name the AST gate matched
        }

    Raises ``ValueError`` on: empty/missing spec, unparseable LLM
    output (after one fence-strip), required function not defined.
    """

    COMPONENT_TYPE: str = "code_write_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return CodeWriteStepConfig

    @classmethod
    def extract_component_config(cls, config: CodeWriteStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "require_function_name": config.require_function_name,
            "default_function_name": config.default_function_name,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: CodeWriteStepConfig,
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
            # Surface at boot rather than first process() call —
            # operators want config errors at workflow-load time, not
            # mid-run.
            raise ValueError(
                f"CodeWriteStep {self.name!r}: failed to read system prompt at {prompt_path}: {e}"
            ) from e

        if not self._system_prompt.strip():
            raise ValueError(
                f"CodeWriteStep {self.name!r}: system prompt at "
                f"{prompt_path} is empty — refusing to LLM-generate "
                f"code with no instructions"
            )

        self._temperature: float = float(component_config["temperature"])
        self._max_tokens: int = int(component_config["max_tokens"])
        self._require_function_name: bool = bool(component_config["require_function_name"])
        self._default_function_name: str | None = component_config.get("default_function_name")

    @staticmethod
    def _resolve_prompt_path(configured: str | None, source_path: str | None) -> Path:
        if configured is None:
            return _DEFAULT_PROMPT_PATH
        p = Path(configured)
        if p.is_absolute():
            return p
        # Relative to the wrapper YAML's directory.
        if source_path:
            return (Path(source_path).resolve().parent / p).resolve()
        # Last resort: cwd.
        return (Path.cwd() / p).resolve()

    @property
    def system_prompt(self) -> str:
        """The loaded system prompt — exposed for tests."""
        return self._system_prompt

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"CodeWriteStep {self.name!r}: input_data must be a dict, "
                f"got {type(input_data).__name__}"
            )

        # Unwrap framework-wrapped input shape, matching RagSynthesisStep.
        if (
            "code_write_input" in input_data
            and isinstance(input_data["code_write_input"], dict)
            and "code_spec" not in input_data
        ):
            input_data = input_data["code_write_input"]

        spec = input_data.get("code_spec")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(
                f"CodeWriteStep {self.name!r}: input_data['code_spec'] "
                f"must be a non-empty string, got "
                f"{type(spec).__name__}={spec!r}"
            )

        function_name = input_data.get("function_name") or self._default_function_name
        if self._require_function_name and not function_name:
            raise ValueError(
                f"CodeWriteStep {self.name!r}: require_function_name=True "
                f"but neither input_data['function_name'] nor "
                f"default_function_name was supplied. Either provide one "
                f"or set require_function_name: false in the step config."
            )

        user_message = self._build_user_message(
            spec=spec,
            function_name=function_name,
            signature=input_data.get("function_signature"),
            previous_attempt=input_data.get("previous_attempt"),
            critique=input_data.get("critique"),
        )

        raw = await asyncio.to_thread(self._invoke_llm, user_message=user_message)

        code_source = self._strip_fences(raw)
        verified_name = self._validate_ast(code_source, expected_function_name=function_name)

        log.info(
            "CodeWriteStep %r: generated %d-char source (function=%r, "
            "temperature=%s, max_tokens=%d)",
            self.name,
            len(code_source),
            verified_name,
            self._temperature,
            self._max_tokens,
        )

        return {
            "code_source": code_source,
            "function_name_verified": verified_name,
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
                f"CodeWriteStep {self.name!r}: LLM returned non-string "
                f"content {type(content).__name__}"
            )
        return content

    @staticmethod
    def _build_user_message(
        *,
        spec: str,
        function_name: str | None,
        signature: str | None,
        previous_attempt: str | None,
        critique: str | None,
    ) -> str:
        parts: list[str] = [f"spec: {spec.strip()}"]
        if function_name:
            parts.append(f"function_name: {function_name}")
        if signature:
            parts.append(f"signature: {signature.strip()}")
        if previous_attempt:
            parts.append(
                "previous_attempt (refinement input — modify this; do "
                "NOT rewrite from scratch):\n" + previous_attempt.strip()
            )
        if critique:
            parts.append("critique (address every point):\n" + critique.strip())
        return "\n\n".join(parts)

    @staticmethod
    def _strip_fences(raw: str) -> str:
        """Remove leading/trailing ``` ```python fences if present.

        Done ONCE — if the output still doesn't parse afterwards,
        that's an LLM-drift signal and we raise. Stripping
        repeatedly or trying multiple parse strategies would mask
        drift that should be surfaced.
        """
        s = raw.strip()
        s = _FENCE_OPEN_PATTERN.sub("", s, count=1)
        s = _FENCE_CLOSE_PATTERN.sub("", s, count=1)
        return s.strip() + "\n"

    def _validate_ast(self, code_source: str, *, expected_function_name: str | None) -> str | None:
        """Parse + check function definition. Return the matched name
        (or None when require_function_name=False and no name supplied)."""
        if not code_source.strip():
            raise ValueError(
                f"CodeWriteStep {self.name!r}: LLM produced empty output "
                f"(no Python source after fence-strip). The system "
                f"prompt may have been misinterpreted; consider a "
                f"sterner spec or a different model."
            )

        try:
            tree = ast.parse(code_source)
        except SyntaxError as e:
            raise ValueError(
                f"CodeWriteStep {self.name!r}: LLM output is not valid "
                f"Python (line {e.lineno}: {e.msg!r}). Likely prose "
                f"output or partial code. Output snippet:\n"
                f"{code_source[:500]!r}"
            ) from e

        if not self._require_function_name:
            return None

        if expected_function_name is None:
            return None

        defined_names = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if expected_function_name not in defined_names:
            raise ValueError(
                f"CodeWriteStep {self.name!r}: LLM output does not "
                f"define expected function {expected_function_name!r}. "
                f"Defined functions: {defined_names}. "
                f"Output snippet:\n{code_source[:500]!r}"
            )

        return expected_function_name


__all__ = ["CodeWriteStep", "CodeWriteStepConfig"]
