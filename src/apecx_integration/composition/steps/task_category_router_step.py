"""TaskCategoryRouterStep — deterministic retrieval-grounded enrichment.

Implements the user-requested "retrieval-grounded codegen" +
"per-task-class curated prompts" for nanobrain-native problems in
a single step. The categories are few (5) and discrete, so the
retrieval is keyword classification + per-category file lookup —
no embeddings or vector store required.

Architecture
------------

```
workflow_input
    → TaskCategoryRouterStep (deterministic)
        - classifies prompt + entry_point into one of:
            step / tool / config / builder / default
        - reads the corresponding example file
        - enriches code_spec with the worked example
    → drafter (LLM)
```

This addresses F14's "the reviser ignores critique" finding by
putting the worked example in the DRAFTER's prompt (where critique
is generally honored), not in a downstream reviser's. The example
is a positive pattern the drafter can imitate, not a negative
critique the model might pattern-match the wrong way around.

Classification rules
--------------------

Keyword matching, ordered (first match wins):

1. ``builder`` — entry_point is ``build_workflow`` OR prompt mentions
   ``WorkflowBuilder``.
2. ``config`` — entry_point ends in ``StepConfig`` OR prompt mentions
   ``StepConfig`` subclass.
3. ``tool`` — entry_point contains ``Tool`` (e.g., ``CalculatorTool``)
   OR prompt mentions ``ToolBase``.
4. ``step`` — entry_point contains ``Step`` (e.g., ``UpperStep``,
   ``ThresholdStep``) OR prompt mentions ``BaseStep``.
5. ``default`` — none of the above; falls back to nanobrain_rules.md.

Note: the order matters. ``config`` is checked BEFORE ``step``
because ``ThresholdStepConfig`` should route to config (more
specific) even though it contains the substring ``Step``.

I/O contract
------------

Input::

    {"code_spec": str, "entry_point"?: str, "test_hint"?: str,
     "function_signature"?: str}

Output::

    {"code_spec": "<original>\\n\\n<worked example>",
     "task_category": "step" | "tool" | "config" | "builder" | "default",
     "entry_point", "test_hint", "function_signature": passthrough}

Silent-failure discipline
-------------------------

* Empty ``code_spec`` → ``ValueError``.
* Missing per-category file → ``ValueError`` at config-load time
  (the step reads ALL example files at init, so a missing file
  fails loud immediately rather than per-call).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "code_writing_prompts"

_CATEGORY_FILES: dict[str, str] = {
    "step": "example_step.md",
    "tool": "example_tool.md",
    "config": "example_config.md",
    "builder": "example_builder.md",
    "default": "nanobrain_rules.md",
}


class TaskCategoryRouterStepConfig(StepConfig):
    """Configuration for TaskCategoryRouterStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    examples_dir: str | None = Field(
        default=None,
        description=(
            "Override the directory containing example_*.md + "
            "nanobrain_rules.md. Defaults to "
            "``composition/code_writing_prompts/``."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class TaskCategoryRouterStep(BaseStep):
    """Deterministic category classifier + example enricher."""

    COMPONENT_TYPE: str = "task_category_router_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return TaskCategoryRouterStepConfig

    @classmethod
    def extract_component_config(cls, config: TaskCategoryRouterStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "examples_dir": config.examples_dir,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: TaskCategoryRouterStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)

        examples_dir = self._resolve_examples_dir(
            component_config.get("examples_dir"),
            component_config.get("source_path"),
        )

        # FAIL-FAST: read all category files at init. A missing file
        # would surface here, not on first process() call.
        self._examples: dict[str, str] = {}
        for category, filename in _CATEGORY_FILES.items():
            path = examples_dir / filename
            if not path.is_file():
                raise ValueError(
                    f"TaskCategoryRouterStep {self.name!r}: example file "
                    f"missing for category={category!r}: {path}"
                )
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError(
                    f"TaskCategoryRouterStep {self.name!r}: example file "
                    f"for category={category!r} is empty: {path}"
                )
            self._examples[category] = text

    @staticmethod
    def _resolve_examples_dir(configured: str | None, source_path: str | None) -> Path:
        if configured is None:
            return _PROMPTS_DIR
        p = Path(configured)
        if p.is_absolute():
            return p
        if source_path:
            return (Path(source_path).resolve().parent / p).resolve()
        return (Path.cwd() / p).resolve()

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(f"TaskCategoryRouterStep {self.name!r}: input_data must be a dict")

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
            raise ValueError(f"TaskCategoryRouterStep {self.name!r}: empty code_spec")

        entry_point = input_data.get("entry_point") or ""
        category = self._classify(spec=spec, entry_point=entry_point)
        example = self._examples[category]

        enriched_spec = f"{spec.strip()}\n\n{example.strip()}\n"

        log.info(
            "TaskCategoryRouterStep %r: category=%r (entry_point=%r)",
            self.name,
            category,
            entry_point,
        )

        return {
            "code_spec": enriched_spec,
            "task_category": category,
            "entry_point": entry_point or None,
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
        }

    @staticmethod
    def _classify(*, spec: str, entry_point: str) -> str:
        """First-match-wins keyword classification.

        Order matters: ``config`` must be checked BEFORE ``step``
        because ``ThresholdStepConfig`` should route to config even
        though it contains ``Step``.
        """
        spec_lower = spec.lower()

        # 1. builder
        if entry_point == "build_workflow" or "workflowbuilder" in spec_lower:
            return "builder"
        # 2. config (must precede step — ThresholdStepConfig)
        if (
            entry_point.endswith("Config")
            or re.search(r"\bStepConfig\b", spec)
            or re.search(r"\b(?:Sub|extend|subclass)\b.*\bStepConfig\b", spec, re.IGNORECASE)
        ):
            return "config"
        # 3. tool
        if "tool" in entry_point.lower() or "toolbase" in spec_lower:
            return "tool"
        # 4. step
        if "step" in entry_point.lower() or re.search(r"\bBaseStep\b", spec):
            return "step"
        # 5. default
        return "default"


__all__ = ["TaskCategoryRouterStep", "TaskCategoryRouterStepConfig"]
