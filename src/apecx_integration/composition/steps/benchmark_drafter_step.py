"""BenchmarkDrafterStep — single LLM call for benchmark code generation.

Why a separate class from ``CodeWriteStep``
-------------------------------------------

``CodeWriteStep`` (the composer's general-purpose authoring step) has
strict downstream contract: it AST-validates that a named function is
defined, requires structured input dict shape, and emits passthrough
fields for the composer's review-revise scaffold. None of that applies
to benchmark codegens where:

- The function name is sometimes inferred from the spec (MBPP) or
  emitted as part of a free-form module (SciCode).
- Test cases call top-level code that may not be a single named
  function.
- The output we need is one string (the candidate code) consumed by
  the benchmark runner's sandbox.

Trying to bend ``CodeWriteStep`` to fit produces a worse contract for
both surfaces. ``BenchmarkDrafterStep`` is the purpose-built
benchmark-side counterpart: same LLM mechanics, simpler I/O shape, and
explicit per-role model binding so a workflow YAML can specify
``role: drafter`` and the step reads the right model from
``model_roles`` without env-var gymnastics.

The CLOSED-CLASS rule (workspace CLAUDE.md, 2026-05-12) is preserved:
this step is the BENCHMARK adapter; the COMPOSER continues to use
``CodeWriteStep``. They share the prompt-loading idiom and the
``build_chat_llm`` plumbing but ARE different classes.

Input/output contract
---------------------

Input ``process(input_data)``: a dict with at minimum::

    {"code_spec": "<prompt for the LLM>"}

Optional fields::

    "entry_point":     str   # function name the runner expects
    "test_hint":       str   # first assert / signature line
    "function_signature": str

Output::

    {"code_source": "<extracted python source>"}

The runner-side adapter (``tests/benchmarks/codegen/nanobrain_workflow.py``)
unwraps the dict to a string.

Silent-failure discipline
-------------------------

1. Empty ``code_spec`` → ``ValueError`` at process() entry. (No silent
   LLM call on empty input.)
2. LLM returns prose-only output with no python fence → the fence
   pattern returns the raw response; the runner's sandbox then
   surfaces a ``SyntaxError`` cleanly. We do NOT raise here — the
   benchmark scorer is the right place to count "model refused to
   produce code" as a fail (it's a real evaluation signal).
3. LLM returns a string of zero length → ``ValueError`` here, because
   that means the LLM call itself broke (vs. produced parseable-but-
   wrong output, which is the model's fault and counts as a fail).

Source-path-relative prompt resolution mirrors ``CodeWriteStep`` so a
wrapper YAML can use ``system_prompt_file: "../prompts/x.md"``.
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

log = logging.getLogger(__name__)


_FENCE_PATTERN = re.compile(r"```(?:python|py)?\s*\n(.*?)\n\s*```", re.DOTALL)

# Roles recognized by the workspace's benchmark codegens. Mirrors the
# resolver in tests/benchmarks/model_roles.py — a step that names a
# role NOT in this list fails at config-load via the validator below
# rather than silently sending requests to a hardcoded fallback model.
_KNOWN_ROLES: frozenset[str] = frozenset({"drafter", "planner", "reviewer"})


def _resolve_role_model(role: str) -> tuple[str, str]:
    """Resolve ``(model, base_url)`` for ``role`` using the same
    rules as ``tests/benchmarks/model_roles.resolve_role``.

    The benchmark resolver lives under ``tests/`` so production-side
    callers can't import it. We mirror its layering here:

      1. ``APECX_LLM_MODEL_<ROLE>`` env var.
      2. ``ComposerConfig.model_roles[<role>]`` from
         ``composer_config.yml``.
      3. ``APECX_LLM_MODEL`` env var.
      4. Hardcoded role default (matches the benchmark resolver).

    Returns concrete strings (never None for base_url).
    """
    import os  # noqa: PLC0415

    env_role = os.environ.get(f"APECX_LLM_MODEL_{role.upper()}")
    model: str | None = env_role

    if model is None:
        try:
            import yaml  # noqa: PLC0415

            config_path = Path(__file__).resolve().parent.parent / "composer_config.yml"
            raw = yaml.safe_load(config_path.read_text())
            roles_map = (raw or {}).get("model_roles") or {}
            entry = roles_map.get(role)
            if isinstance(entry, dict) and entry.get("model"):
                model = entry["model"]
        except Exception:
            model = None

    if not model:
        model = os.environ.get("APECX_LLM_MODEL")

    if not model:
        # Hardcoded fallback per role.
        _hardcoded = {
            "drafter": "mistral-nemo:latest",
            "planner": "nemotron-3-nano:4b",
            "reviewer": "nemotron-3-nano:4b",
        }
        model = _hardcoded[role]

    base = os.environ.get("APECX_LLM_BASE_URL") or "http://localhost:11434/v1"
    return model, base


class BenchmarkDrafterStepConfig(StepConfig):
    """Configuration for ``BenchmarkDrafterStep``.

    ``extra='forbid'`` (workspace rule): YAML typos fail at config
    load, not silently as defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # ConfigBase populates this; declared so extra='forbid' accepts it.
    source_path: str | None = Field(default=None)

    system_prompt_file: str | None = Field(
        default=None,
        description=(
            "Path to the system prompt markdown file. Relative paths "
            "are resolved against this YAML's directory. The default "
            "bundled prompt lives at "
            "``composition/code_writing_prompts/benchmark_drafter_system.md``."
        ),
    )

    rules_file: str | None = Field(
        default=None,
        description=(
            "Optional path to a supplementary rules file (e.g., "
            "``nanobrain_rules.md``) that is appended to the system "
            "prompt at step-load time. Use when the task domain "
            "(framework-native code) has conventions the bare "
            "system prompt does not encode. Resolved like "
            "``system_prompt_file`` (relative to wrapper YAML)."
        ),
    )

    role: str = Field(
        default="drafter",
        description=(
            "Which entry in ``composer_config.yml`` model_roles to use. "
            "One of ``drafter``, ``planner``, ``reviewer``."
        ),
    )

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64)

    request_timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        description=(
            "Hard wall-clock cap on a single LLM HTTP call. The framework's "
            "Workflow.wait_for_cascade does NOT hard-cap LLM calls — its "
            "timeout is a settle-quiet probe, not a request budget. Without "
            "a request-level timeout, a thinking-token model (nemotron, "
            "deepseek-r1) emitting endless ``<think>`` blocks can hang a "
            "sweep for 5+ minutes per problem. 60s default is generous for "
            "drafters; planners on small thinking models should drop this "
            "to 45s. Source: 2026-05-12 plan-then-code n=50 sweep, where "
            "mbpp/84-94 each hung ~420s under the 120s cascade timeout."
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
                f"BenchmarkDrafterStepConfig: role={self.role!r} is not "
                f"one of {sorted(_KNOWN_ROLES)}. Add it to "
                f"benchmark_drafter_step._KNOWN_ROLES if intended."
            )
        return self


_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "benchmark_drafter_system.md"
)


class BenchmarkDrafterStep(BaseStep):
    """Single-call LLM drafter for benchmark problems.

    See module docstring for the I/O contract.
    """

    COMPONENT_TYPE: str = "benchmark_drafter_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return BenchmarkDrafterStepConfig

    @classmethod
    def extract_component_config(cls, config: BenchmarkDrafterStepConfig) -> dict[str, Any]:
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
        config: BenchmarkDrafterStepConfig,
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
                f"BenchmarkDrafterStep {self.name!r}: failed to read system "
                f"prompt at {prompt_path}: {e}"
            ) from e
        if not self._system_prompt.strip():
            raise ValueError(
                f"BenchmarkDrafterStep {self.name!r}: system prompt at "
                f"{prompt_path} is empty — refusing to LLM-generate code "
                f"with no instructions"
            )

        # Optional rules-file append. Use case: nanobrain-native problems
        # need framework-specific guidance (don't override from_config,
        # correct imports, etc.) that would bloat MBPP/SciCode prompts
        # without helping. Per-workflow choice via wrapper YAML.
        rules_path = self._resolve_rules_path(
            component_config.get("rules_file"),
            component_config.get("source_path"),
        )
        if rules_path is not None:
            try:
                rules_text = rules_path.read_text(encoding="utf-8")
            except OSError as e:
                raise ValueError(
                    f"BenchmarkDrafterStep {self.name!r}: failed to read "
                    f"rules file at {rules_path}: {e}"
                ) from e
            if rules_text.strip():
                # Append with a clear delimiter so the LLM treats it as
                # additional instructions, not a continuation.
                self._system_prompt = (
                    self._system_prompt.rstrip() + "\n\n---\n\n" + rules_text.strip() + "\n"
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

    @staticmethod
    def _resolve_rules_path(configured: str | None, source_path: str | None) -> Path | None:
        """Mirror of ``_resolve_prompt_path`` for the optional rules file.

        Returns None when ``configured`` is None (no rules to append).
        Otherwise resolves the path the same way as the prompt file.
        """
        if configured is None:
            return None
        p = Path(configured)
        if p.is_absolute():
            return p
        if source_path:
            return (Path(source_path).resolve().parent / p).resolve()
        return (Path.cwd() / p).resolve()

    @property
    def system_prompt(self) -> str:
        """Loaded system prompt — exposed for tests + LLM-input introspection."""
        return self._system_prompt

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"BenchmarkDrafterStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )

        # Framework-trigger-payload unwrap: when the workflow's
        # DirectLink delivers via the trigger system, input_data is
        # wrapped {<input_du_name>: <payload>}. Detect + unwrap.
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
                f"BenchmarkDrafterStep {self.name!r}: input_data['code_spec'] "
                f"(or 'problem_prompt') must be a non-empty string, got "
                f"{type(spec).__name__}={spec!r}"
            )

        user_message = self._build_user_message(
            spec=spec,
            entry_point=input_data.get("entry_point"),
            test_hint=input_data.get("test_hint"),
            function_signature=input_data.get("function_signature"),
            previous_attempt=input_data.get("previous_attempt"),
            critique=input_data.get("critique"),
        )

        model, base_url = _resolve_role_model(self._role)
        raw = await asyncio.to_thread(
            self._invoke_llm,
            user_message=user_message,
            model=model,
            base_url=base_url,
        )

        code_source = self._extract_code(raw)
        if not code_source.strip():
            raise ValueError(
                f"BenchmarkDrafterStep {self.name!r}: LLM returned empty "
                f"response (model={model!r}). Likely transient LLM error; "
                f"not silently shipping empty code downstream."
            )

        log.info(
            "BenchmarkDrafterStep %r: generated %d-char source (role=%r, "
            "model=%r, temperature=%s, max_tokens=%d)",
            self.name,
            len(code_source),
            self._role,
            model,
            self._temperature,
            self._max_tokens,
        )

        # Passthrough fields: include inputs verbatim alongside
        # ``code_source`` so a downstream reviewer/reviser step wired
        # via a single DirectLink can read BOTH the generated code AND
        # the original spec. Avoids inventing a context-assembly step.
        return {
            "code_source": code_source,
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
            raise ValueError(
                f"BenchmarkDrafterStep {self.name!r}: LLM returned non-string "
                f"content {type(content).__name__}"
            )
        return content

    @staticmethod
    def _build_user_message(
        *,
        spec: str,
        entry_point: str | None,
        test_hint: str | None,
        function_signature: str | None,
        previous_attempt: str | None = None,
        critique: str | None = None,
    ) -> str:
        parts: list[str] = [spec.strip()]
        if function_signature:
            parts.append(f"Function signature:\n{function_signature.strip()}")
        if entry_point:
            parts.append(f"Define a function named ``{entry_point}``.")
        if test_hint:
            parts.append(f"Your code must satisfy:\n{test_hint.strip()}")
        if previous_attempt:
            parts.append(
                "Your previous attempt (revise it; do NOT rewrite from "
                "scratch):\n```python\n" + previous_attempt.strip() + "\n```"
            )
        if critique:
            parts.append(
                "Reviewer critique — address every point in your revision:\n" + critique.strip()
            )
        return "\n\n".join(parts)

    @staticmethod
    def _extract_code(raw: str) -> str:
        """Pull the largest ```python ... ``` block; fall back to the
        raw string. Strip ``<think>...</think>`` blocks emitted by
        thinking-token models (nemotron / qwen / deepseek-r1) before
        the fence match.
        """
        # Strip think blocks first — they may contain ``` fences inside,
        # which would confuse the largest-block heuristic below.
        cleaned = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL | re.IGNORECASE)
        candidates = _FENCE_PATTERN.findall(cleaned)
        if candidates:
            return max(candidates, key=len)
        return cleaned.strip()


__all__ = ["BenchmarkDrafterStep", "BenchmarkDrafterStepConfig"]
