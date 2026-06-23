"""CodeStructureValidatorStep — deterministic AST inspector that gates the
review-revise scaffold.

The hybrid-scaffold pattern (CGU-P2-T1b): mix LLM agents (drafter,
reviser) with deterministic steps (this one). The validator parses
the candidate's source with Python's ``ast`` module and runs specific
framework-violation checks. It emits ``decision: pass`` when the code
looks structurally correct OR ``decision: fix`` with a deterministic
critique when it spots a known failure shape.

Why deterministic, not LLM
--------------------------

The session's LLM-reviewer experiment (review-revise v0) regressed the
nanobrain-native pass@1 by 30pp because the reviewer hallucinated
problems with correct code and the reviser "fixed" them into breaking.
The fix is to replace the LLM reviewer with a deterministic check that
has 100% precision: it only complains when a real AST-detectable
problem exists. The downstream reviser then operates on grounded
critiques, not LLM hallucinations.

Failure shapes the validator catches (with 100% precision)
----------------------------------------------------------

1. **Unparseable code** — ``SyntaxError`` during ``ast.parse``.
2. **Missing entry point** — when ``entry_point`` is supplied and is
   not present as a class or function name at module scope.
3. **``from_config`` override** — the LLM frequently defines a
   ``@classmethod def from_config(cls, ...)`` inside a class that
   inherits ``BaseStep``/``ToolBase``/``Workflow``, which triggers
   ``RuntimeError: Direct instantiation prohibited`` at load time.
4. **``execute`` override** — overriding ``execute()`` on a BaseStep
   subclass raises ``ComponentConfigurationError`` at framework
   init time.
5. **Hallucinated imports** — ``from nanobrain.utils``,
   ``from nanobrain.helpers`` (no such packages).

For nanobrain-native problems the entry_point is the expected class
name; the validator uses it to check #2 + identify which class to
inspect for #3/#4. For MBPP-class problems the entry_point may be a
function name; #3/#4 don't apply (no class to inspect), and the
validator's only useful check is #1 (syntax) — pass-through otherwise.

I/O contract
------------

Input (after envelope unwrap)::

    {"code_spec": str, "code_source": str,
     "entry_point"?: str, "test_hint"?: str,
     "function_signature"?: str}

Output (always)::

    {"decision": "pass" | "fix",
     "code_source": <passthrough — used by ConditionalLink pass path>,
     "previous_attempt": <was code_source — used by reviser>,
     "critique": "<deterministic critique or 'PASS'>",
     "code_spec", "entry_point", "test_hint", "function_signature": passthrough}

Silent-failure discipline
-------------------------

* Empty ``code_source`` → ``ValueError`` (cannot validate nothing).
* AST parse error on a non-empty input → ``decision: fix`` with a
  specific "SyntaxError at line N" critique (NOT a raise — the
  reviser can fix syntax).
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.novel_python_validation import (
    validate_python_structure,
)

log = logging.getLogger(__name__)


class CodeStructureValidatorStepConfig(StepConfig):
    """Configuration for CodeStructureValidatorStep.

    Deterministic step — no LLM, no temperature, no max_tokens.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    strict_imports: bool = Field(
        default=True,
        description=(
            "When True (default), imports from non-whitelisted "
            "nanobrain.* submodules raise a critique. Disable for "
            "loaders that legitimately use library subpaths."
        ),
    )

    require_process_on_step_subclasses: bool = Field(
        default=False,
        description=(
            "When True, a class that inherits BaseStep must define "
            "``async def process``. Defaulting False because the "
            "absence of process() will be caught by the runtime "
            "framework anyway and false positives are harder to "
            "swallow than false negatives here."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class CodeStructureValidatorStep(BaseStep):
    """Deterministic AST-based validator. See module docstring."""

    COMPONENT_TYPE: str = "code_structure_validator_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return CodeStructureValidatorStepConfig

    @classmethod
    def extract_component_config(cls, config: CodeStructureValidatorStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "strict_imports": config.strict_imports,
            "require_process_on_step_subclasses": config.require_process_on_step_subclasses,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: CodeStructureValidatorStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._strict_imports: bool = bool(component_config["strict_imports"])
        self._require_process: bool = bool(component_config["require_process_on_step_subclasses"])

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"CodeStructureValidatorStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )

        # Trigger-envelope unwrap.
        if (
            len(input_data) == 1
            and "code_source" not in input_data
            and "code_spec" not in input_data
        ):
            (only_key,) = input_data.keys()
            if isinstance(input_data[only_key], dict):
                input_data = input_data[only_key]

        candidate = input_data.get("code_source")
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError(
                f"CodeStructureValidatorStep {self.name!r}: input_data['code_source'] "
                f"must be a non-empty string (cannot validate nothing)"
            )

        entry_point = input_data.get("entry_point") or ""
        issues = self._validate(candidate, entry_point)

        if not issues:
            decision = "pass"
            critique = "PASS"
        else:
            decision = "fix"
            critique = "\n".join(f"- {issue}" for issue in issues)

        log.info(
            "CodeStructureValidatorStep %r: decision=%s, issues=%d",
            self.name,
            decision,
            len(issues),
        )

        return {
            "decision": decision,
            "code_source": candidate,
            "previous_attempt": candidate,
            "critique": critique,
            "code_spec": input_data.get("code_spec"),
            "entry_point": entry_point,
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
        }

    def _validate(self, code: str, entry_point: str) -> list[str]:
        """Run AST structural checks (delegates to the shared validator).

        Returns an ordered list of issue strings; empty = passes. The shared
        ``validate_python_structure`` is the single source of truth, also used
        by the composer's novel-Python acceptance check.
        """
        return validate_python_structure(
            code,
            entry_point,
            strict_imports=self._strict_imports,
            require_process=self._require_process,
        )


__all__ = [
    "CodeStructureValidatorStep",
    "CodeStructureValidatorStepConfig",
]
