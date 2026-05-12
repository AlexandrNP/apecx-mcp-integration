"""TestWriteStep — LLM-backed pytest authoring.

Pair with ``CodeWriteStep`` for a generate-code-then-generate-tests
flow. Same shape as CodeWriteStep: ``from_config``, system prompt
file, AST gate, fence-strip. Output: ``test_code`` (a string) plus
the source it was generated for (passthrough so a downstream
``IsolatedPyExecStep`` can run code + tests in one subprocess).

Silent-failure discipline:

  1. LLM emits prose-only → ``ast.parse`` fails → ``ValueError``.
  2. LLM emits code that defines no ``test_*`` function → ``ValueError``.
  3. LLM wraps in ``` ```python fences → stripped ONCE then re-parsed.
  4. Empty / missing ``code_source`` → ``ValueError``.
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

_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "test_writer_system.md"
)

_FENCE_OPEN_PATTERN = re.compile(r"^\s*```(?:python|py)?\s*\n", re.MULTILINE)
_FENCE_CLOSE_PATTERN = re.compile(r"\n```\s*$", re.MULTILINE)


class TestWriteStepConfig(StepConfig):
    """Configuration for TestWriteStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to the test-writer system prompt. Defaults to the "
            "bundled code_writing_prompts/test_writer_system.md."
        ),
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64)
    min_tests: int = Field(
        default=1,
        ge=1,
        description=(
            "Minimum number of test_* functions the LLM must produce. "
            "When fewer are found, the step raises rather than ships "
            "an under-tested artifact."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class TestWriteStep(BaseStep):
    """Author pytest tests for a given function.

    Note ``__test__ = False`` below: pytest auto-collects classes
    whose name starts with ``Test`` as test classes; we set the flag
    to suppress the resulting collection warning. This class is
    application code, not a test suite.

    Expected ``process()`` input::

        {
            "code_source": "def fib(n: int) -> int: ...",
            "code_spec": "Write a fibonacci function with base cases f(0)=0, f(1)=1.",
            "function_name": "fib",   # optional
        }

    Return shape::

        {
            "test_code": "def test_fib_base_case():\\n    assert fib(0) == 0\\n...",
            "test_function_count": 3,
            "code_source": "...",    # passthrough so a downstream exec
            "code_spec":   "...",    # step can run code + tests together
        }
    """

    COMPONENT_TYPE: str = "test_write_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]
    __test__ = False

    @classmethod
    def _get_config_class(cls):
        return TestWriteStepConfig

    @classmethod
    def extract_component_config(cls, config: TestWriteStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "min_tests": config.min_tests,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: TestWriteStepConfig,
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
                f"TestWriteStep {self.name!r}: failed to read system prompt at {prompt_path}: {e}"
            ) from e
        if not self._system_prompt.strip():
            raise ValueError(
                f"TestWriteStep {self.name!r}: system prompt at {prompt_path} is empty"
            )
        self._temperature: float = float(component_config["temperature"])
        self._max_tokens: int = int(component_config["max_tokens"])
        self._min_tests: int = int(component_config["min_tests"])

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
                f"TestWriteStep {self.name!r}: input_data must be a dict, "
                f"got {type(input_data).__name__}"
            )
        if (
            "test_write_input" in input_data
            and isinstance(input_data["test_write_input"], dict)
            and "code_source" not in input_data
        ):
            input_data = input_data["test_write_input"]

        code = input_data.get("code_source")
        spec = input_data.get("code_spec", "")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(
                f"TestWriteStep {self.name!r}: input_data['code_source'] "
                f"must be a non-empty string, got "
                f"{type(code).__name__}={code!r}"
            )
        function_name = input_data.get("function_name")

        user_message = self._build_user_message(code=code, spec=spec, function_name=function_name)
        raw = await asyncio.to_thread(self._invoke_llm, user_message=user_message)
        test_code = self._strip_fences(raw)
        test_function_count = self._validate_tests(test_code)

        log.info(
            "TestWriteStep %r: produced %d-char tests with %d test_* functions",
            self.name,
            len(test_code),
            test_function_count,
        )
        return {
            "test_code": test_code,
            "test_function_count": test_function_count,
            # Passthrough so a downstream IsolatedPyExecStep can run
            # the code AND tests in one subprocess (code_source +
            # test_code concatenated).
            "code_source": code,
            "code_spec": spec,
            "function_name": function_name,
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
                f"TestWriteStep {self.name!r}: LLM returned non-string "
                f"content {type(content).__name__}"
            )
        return content

    @staticmethod
    def _build_user_message(*, code: str, spec: str, function_name: str | None) -> str:
        parts = [
            f"spec: {spec.strip() if spec else '<no spec provided>'}",
            "code:\n" + code.rstrip(),
        ]
        if function_name:
            parts.append(f"function_name: {function_name}")
        return "\n\n".join(parts)

    @staticmethod
    def _strip_fences(raw: str) -> str:
        s = raw.strip()
        s = _FENCE_OPEN_PATTERN.sub("", s, count=1)
        s = _FENCE_CLOSE_PATTERN.sub("", s, count=1)
        return s.strip() + "\n"

    def _validate_tests(self, test_code: str) -> int:
        if not test_code.strip():
            raise ValueError(f"TestWriteStep {self.name!r}: LLM produced empty output.")
        try:
            tree = ast.parse(test_code)
        except SyntaxError as e:
            raise ValueError(
                f"TestWriteStep {self.name!r}: LLM output is not valid "
                f"Python (line {e.lineno}: {e.msg!r}). Snippet: "
                f"{test_code[:500]!r}"
            ) from e
        test_funcs = [
            n.name
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
        ]
        if len(test_funcs) < self._min_tests:
            raise ValueError(
                f"TestWriteStep {self.name!r}: LLM produced "
                f"{len(test_funcs)} test_* function(s), expected at "
                f"least {self._min_tests}. Found functions: "
                f"{[n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]}"
            )
        return len(test_funcs)


__all__ = ["TestWriteStep", "TestWriteStepConfig"]
