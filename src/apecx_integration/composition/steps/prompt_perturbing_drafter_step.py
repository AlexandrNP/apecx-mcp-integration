"""PromptPerturbingDrafterStep — strong-form SGDe structural-consensus drafter.

Item 2 of the post-F17 backlog. F18 measured the WEAK form of SGDe's
structural consensus (temperature-variance fan-out via
``MultiSampleDrafterStep``) at -10pp vs F17 on nanobrain-native n=10.
The brutal-truth diagnosis: temperature alone does not cover the
correct shape distribution for the 3 hard problems on mistral-nemo 12B.

This step replaces temperature-variance with **prompt-variance**. Each
of N parallel samples sees a DIFFERENT user-message stem phrase, so
the drafter's reasoning trajectory diverges along the perturbation
axis even at temperature=0. SGDe's GSM-Hard +26-34pp claim depends
on this design — not on temperature.

How it differs from ``MultiSampleDrafterStep``
----------------------------------------------

* ``MultiSampleDrafterStep``: N parallel calls, SAME prompt, T>0 for
  sampling variance. FAIL-FAST on T=0 + N>1.
* ``PromptPerturbingDrafterStep``: N parallel calls, DIFFERENT prompts
  (one per perturbation), T can be 0. FAIL-FAST if N > len(perturbations)
  AND temperature is 0 (would produce identical wraparound samples).

The output schema is intentionally identical to ``MultiSampleDrafterStep``
so the existing ``ConsensusAggregatorStep`` consumes its output without
any change. Drop-in replacement at the workflow level.

Why stem perturbation specifically
----------------------------------

Three legitimate perturbation axes exist (cf. SGDe §III):

1. **Stem phrasing** ("Implement" vs "Author" vs "Write") — *this* file.
2. **Worked-example selection** (different ``example_*.md`` per sample)
   — high regression risk on category-specific problems; not shipped here.
3. **Rule-ordering** (re-shuffle ``nanobrain_rules.md`` per sample) —
   folklore-heuristic; not shipped.

Stem phrasing has the lowest regression risk because the SEMANTIC TASK
(write code for the spec) is unchanged across samples; only the
imperative framing varies. This is the most-paper-aligned cheapest
experiment.

I/O contract
------------

Input (same shape as ``MultiSampleDrafterStep``)::

    {"code_spec": str, "entry_point"?: str, "test_hint"?: str,
     "function_signature"?: str, "task_category"?: str}

Output (drop-in compatible with ``ConsensusAggregatorStep``)::

    {"candidates": [{"code_source": str, "perturbation": str}, ...],
     "n_samples": int,
     "temperature": float,
     "model": str,
     "perturbations_used": list[str],
     "code_spec", "entry_point", "test_hint", "function_signature",
     "task_category": passthrough}

Silent-failure discipline
-------------------------

* Empty ``code_spec`` → ``ValueError``.
* All N samples return empty → ``ValueError``.
* Some samples empty → dropped; aggregator handles partial list.
* ``n_samples > len(perturbations)`` AND ``temperature == 0``
  → ``ValueError`` at config-load (identical wraparound samples are
  silent-failure noise — author should either raise temperature OR
  add more perturbations).
* Duplicate entries in ``perturbations`` → ``ValueError`` (no variance).
* ``len(perturbations) < 2`` → ``ValueError`` (a single perturbation
  is just a degenerate ``BenchmarkDrafterStep`` — wrong tool).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents._llm_factory import build_chat_llm
from apecx_integration.composition.steps.benchmark_drafter_step import (
    _resolve_role_model,
)
from apecx_integration.composition.steps.multi_sample_drafter_step import (
    _FENCE_PATTERN,
    _THINK_BLOCK,
)

log = logging.getLogger(__name__)

_KNOWN_ROLES: frozenset[str] = frozenset({"drafter", "planner", "reviewer"})

# Default perturbation set. Three imperative-stem variants chosen to
# cover the most-common framings the model has seen in pre-training
# without changing the underlying semantic task. Authors override
# per workflow.
_DEFAULT_PERTURBATIONS: list[str] = [
    "Implement",
    "Author",
    "Write",
]


class PromptPerturbingDrafterStepConfig(StepConfig):
    """Configuration for ``PromptPerturbingDrafterStep``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    system_prompt_file: str | None = Field(default=None)
    rules_file: str | None = Field(default=None)
    role: str = Field(default="drafter")

    perturbations: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_PERTURBATIONS),
        description=(
            "List of imperative-stem phrases. Each parallel sample uses "
            "one. len(perturbations) >= 2 enforced (a single perturbation "
            "defeats the purpose). Duplicates rejected."
        ),
    )

    n_samples: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of independent samples. 0 (default) means use "
            "len(perturbations) — one sample per perturbation. Setting "
            "n_samples != 0 forces a specific count; if "
            "n_samples > len(perturbations) the extras wrap modulo, which "
            "is only meaningful with temperature > 0 (validated)."
        ),
    )

    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description=(
            "Sampling temperature. 0.0 is fine here because variance "
            "is in the prompt, not the sampler. Raise only if you want "
            "compound prompt+temp variance OR if n_samples > len(perturbations)."
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
    def _validate_perturbations_and_samples(self):
        if self.role not in _KNOWN_ROLES:
            raise ValueError(
                f"PromptPerturbingDrafterStepConfig: role={self.role!r} "
                f"is not one of {sorted(_KNOWN_ROLES)}."
            )
        if len(self.perturbations) < 2:
            raise ValueError(
                f"PromptPerturbingDrafterStepConfig: need >=2 perturbations "
                f"(got {len(self.perturbations)}). A single perturbation is "
                f"a degenerate single-sample drafter; use BenchmarkDrafterStep."
            )
        if any(not p.strip() for p in self.perturbations):
            raise ValueError(
                "PromptPerturbingDrafterStepConfig: perturbation entries must be non-empty strings"
            )
        # Duplicates defeat the variance purpose.
        seen: set[str] = set()
        for p in self.perturbations:
            key = p.strip().lower()
            if key in seen:
                raise ValueError(
                    f"PromptPerturbingDrafterStepConfig: duplicate "
                    f"perturbation {p!r} (case-insensitive). Each must be "
                    f"distinct."
                )
            seen.add(key)
        # n_samples=0 -> default to len(perturbations).
        effective_n = self.n_samples or len(self.perturbations)
        if effective_n > len(self.perturbations) and self.temperature == 0.0:
            raise ValueError(
                f"PromptPerturbingDrafterStepConfig: n_samples={effective_n} > "
                f"len(perturbations)={len(self.perturbations)} with "
                f"temperature=0.0 produces identical wraparound samples. "
                f"Either raise temperature > 0 or add more perturbations."
            )
        return self


_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "code_writing_prompts" / "benchmark_drafter_system.md"
)


class PromptPerturbingDrafterStep(BaseStep):
    """Fan-out drafter with per-sample prompt perturbation (strong SGDe)."""

    COMPONENT_TYPE: str = "prompt_perturbing_drafter_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return PromptPerturbingDrafterStepConfig

    @classmethod
    def extract_component_config(cls, config: PromptPerturbingDrafterStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "system_prompt_file": config.system_prompt_file,
            "rules_file": config.rules_file,
            "role": config.role,
            "perturbations": list(config.perturbations),
            "n_samples": config.n_samples,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "request_timeout_seconds": config.request_timeout_seconds,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: PromptPerturbingDrafterStepConfig,
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
                f"PromptPerturbingDrafterStep {self.name!r}: failed to read "
                f"prompt at {prompt_path}: {e}"
            ) from e

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
                    f"PromptPerturbingDrafterStep {self.name!r}: failed to "
                    f"read rules at {rules_path}: {e}"
                ) from e
            if rules_text.strip():
                self._system_prompt = (
                    self._system_prompt.rstrip() + "\n\n---\n\n" + rules_text.strip() + "\n"
                )

        self._role: str = component_config["role"]
        self._perturbations: list[str] = list(component_config["perturbations"])
        configured_n = int(component_config["n_samples"])
        self._n_samples: int = configured_n or len(self._perturbations)
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

    @property
    def perturbations(self) -> list[str]:
        """Read-only view of the perturbation set used for fan-out."""
        return list(self._perturbations)

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"PromptPerturbingDrafterStep {self.name!r}: input_data must be a dict"
            )

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
            raise ValueError(f"PromptPerturbingDrafterStep {self.name!r}: empty code_spec")

        model, base_url = _resolve_role_model(self._role)

        # Build one user message per sample, varying the stem.
        perturbations_used: list[str] = []
        user_messages: list[str] = []
        for i in range(self._n_samples):
            stem = self._perturbations[i % len(self._perturbations)]
            perturbations_used.append(stem)
            user_messages.append(
                self._build_user_message(
                    stem=stem,
                    spec=spec,
                    entry_point=input_data.get("entry_point"),
                    test_hint=input_data.get("test_hint"),
                    function_signature=input_data.get("function_signature"),
                )
            )

        # Fan-out N parallel LLM calls via asyncio.gather.
        async def _one_sample(msg: str) -> str:
            raw = await asyncio.to_thread(
                self._invoke_llm,
                user_message=msg,
                model=model,
                base_url=base_url,
            )
            return self._extract_code(raw)

        raws: list[str] = await asyncio.gather(*[_one_sample(m) for m in user_messages])

        candidates: list[dict[str, Any]] = []
        for stem, code in zip(perturbations_used, raws, strict=True):
            if code.strip():
                candidates.append({"code_source": code, "perturbation": stem})

        if not candidates:
            raise ValueError(
                f"PromptPerturbingDrafterStep {self.name!r}: all "
                f"{self._n_samples} samples returned empty (model={model!r}, "
                f"perturbations={perturbations_used!r}, T={self._temperature})"
            )

        log.info(
            "PromptPerturbingDrafterStep %r: %d/%d non-empty samples "
            "(role=%r, model=%r, T=%s, perturbations=%s)",
            self.name,
            len(candidates),
            self._n_samples,
            self._role,
            model,
            self._temperature,
            perturbations_used,
        )

        return {
            "candidates": candidates,
            "n_samples": self._n_samples,
            "temperature": self._temperature,
            "model": model,
            "perturbations_used": perturbations_used,
            "code_spec": spec,
            "entry_point": input_data.get("entry_point"),
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
            "task_category": input_data.get("task_category"),
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
        stem: str,
        spec: str,
        entry_point: str | None,
        test_hint: str | None,
        function_signature: str | None,
    ) -> str:
        # Prepend the imperative stem to the original spec. The system
        # prompt instructs the model to emit code; the stem just frames
        # the spec. Sample-to-sample variance comes from the stem AND
        # the model's response trajectory diverging from there.
        parts = [f"{stem} the following:\n\n{spec.strip()}"]
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


__all__ = [
    "PromptPerturbingDrafterStep",
    "PromptPerturbingDrafterStepConfig",
]
