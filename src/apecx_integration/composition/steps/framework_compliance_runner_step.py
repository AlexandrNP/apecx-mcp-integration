"""FrameworkComplianceRunnerStep — deterministic *runtime* validator.

Companion to ``CodeStructureValidatorStep`` (static AST validator).
Where the AST validator catches syntactic shape violations, this
step catches RUNTIME framework-compliance failures by actually
trying to load each class via ``from_config`` and invoke
``process()`` / ``execute()`` in a subprocess.

Failure shapes caught (that the AST validator misses)
-----------------------------------------------------

* ``RuntimeError: Direct instantiation of <Cls> is prohibited`` —
  a custom ``from_config`` that the AST validator's pattern match
  missed (e.g., obscure decorator order).
* ``ComponentConfigurationError`` — framework-level FAIL-FAST when
  ``execute()`` is overridden in a way the AST check missed.
* ``pydantic.ValidationError: extra_forbidden`` — the candidate's
  custom StepConfig sets ``extra='forbid'`` but doesn't strip the
  framework's ``class:`` injection (very common LLM-drift shape).
* ``ImportError`` / ``ModuleNotFoundError`` — hallucinated import
  paths that the AST validator's whitelist missed (catches
  third-party-package hallucinations too).
* ``AttributeError`` on ``process()`` invocation — the candidate
  imports the right base class but calls a non-existent method.

What this step does NOT check
-----------------------------

* Problem-specific BEHAVIOR (does ``UpperStep`` actually uppercase?
  That's the benchmark's hidden test job, not the scaffold's).
* Whether the candidate produces correct output for the spec's
  semantic intent.

I/O contract
------------

Input (after envelope unwrap)::

    {"code_spec": str, "code_source": str,
     "entry_point"?: str, "test_hint"?: str,
     "function_signature"?: str}

Output::

    {"decision": "pass" | "fix",
     "code_source": <passthrough>,
     "previous_attempt": <was code_source>,
     "critique": "<deterministic critique with runtime traceback>",
     "code_spec", "entry_point", "test_hint", "function_signature": passthrough}

Silent-failure discipline
-------------------------

* Empty ``code_source`` → ``ValueError`` (cannot validate nothing).
* Subprocess timeout → ``decision: fix`` with timeout critique
  (NOT a raise — a slow candidate is a candidate to revise).
* Subprocess produces no output AND non-zero exit → ``decision: fix``
  with NonZeroExit critique.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


# Probe budget — generous because subclass instantiation can pull in
# numpy / scipy on SciCode-class candidates. 15s is enough for any
# legitimate framework load; longer = the candidate is misbehaving.
_DEFAULT_PROBE_TIMEOUT: float = 15.0


# The compliance probe script. Inserted into the sandbox as test_code.
# Imports the candidate (already in __main__ from setup_code+candidate_code
# concatenation), iterates classes, tries from_config + invocation.
# Emits a JSON line on stdout encoding the result.
_PROBE_SCRIPT_TEMPLATE = '''
import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path

_failures = []
_successes = []
_target_classes = {target_classes}


def _try_load(cls_name, base_module):
    """Attempt BaseStep.from_config / ToolBase.from_config / Workflow.from_config / BaseAgent.from_config."""
    try:
        if base_module == "step":
            from nanobrain.core.step import BaseStep
            base = BaseStep
        elif base_module == "tool":
            from nanobrain.core.tool import ToolBase
            base = ToolBase
        elif base_module == "workflow":
            from nanobrain.core.workflow import Workflow
            base = Workflow
        elif base_module == "agent":
            from nanobrain.core.agent import BaseAgent
            base = BaseAgent
        else:
            return None, f"unknown base_module {{base_module}}"
    except Exception as e:
        return None, f"{{type(e).__name__}}: {{e}}"

    with tempfile.TemporaryDirectory() as td:
        yaml_path = Path(td) / "s.yml"
        yaml_path.write_text(
            f"class: '__main__.{{cls_name}}'\\nname: probe_{{cls_name.lower()}}\\n"
        )
        try:
            return base.from_config(str(yaml_path)), None
        except Exception as e:
            return None, f"{{type(e).__name__}}: {{e}}"


async def _try_invoke(instance, cls_name):
    if hasattr(instance, "process"):
        try:
            await instance.process({{}})
            return None
        except Exception as e:
            # Some exceptions are expected on empty input (KeyError
            # on missing required key). We DON'T treat KeyError as
            # a framework-compliance failure — that's correct
            # behavior for empty input. Anything else IS a problem.
            if isinstance(e, (KeyError, TypeError, ValueError)) and "input_data" not in str(e).lower():
                # Accept these as expected "empty input rejected"
                return None
            return f"{{type(e).__name__}}: {{str(e)[:200]}}"
    return None


async def _main():
    for cls_name, base_module in _target_classes:
        instance, load_err = _try_load(cls_name, base_module)
        if load_err is not None:
            _failures.append({{"class": cls_name, "stage": "from_config", "error": load_err}})
            continue
        invoke_err = await _try_invoke(instance, cls_name)
        if invoke_err is not None:
            _failures.append({{"class": cls_name, "stage": "process", "error": invoke_err}})
            continue
        _successes.append(cls_name)


try:
    asyncio.run(_main())
except Exception:
    sys.stderr.write(traceback.format_exc())

print("===COMPLIANCE_REPORT===")
print(json.dumps({{"failures": _failures, "successes": _successes}}))
'''


class FrameworkComplianceRunnerStepConfig(StepConfig):
    """Configuration for FrameworkComplianceRunnerStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    probe_timeout_seconds: float = Field(
        default=_DEFAULT_PROBE_TIMEOUT,
        ge=1.0,
        description=(
            "Subprocess wall-clock budget for the compliance probe. "
            "15s default is enough for any legitimate framework load; "
            "longer = the candidate is misbehaving."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


def _indent(text: str, n_spaces: int) -> str:
    """Indent every line of ``text`` by ``n_spaces`` characters.

    Mirrors ``tests/benchmarks/sandbox.py:_indent``. Inlined here to
    keep src/ free of test-package imports.
    """
    if not text or not text.strip():
        return " " * n_spaces + "pass"
    prefix = " " * n_spaces
    return "\n".join(prefix + line for line in text.splitlines())


_FRAMEWORK_BASES_TO_MODULE: dict[str, str] = {
    "BaseStep": "step",
    "ToolBase": "tool",
    "Workflow": "workflow",
    "BaseAgent": "agent",
}


class FrameworkComplianceRunnerStep(BaseStep):
    """Deterministic runtime validator. See module docstring."""

    COMPONENT_TYPE: str = "framework_compliance_runner_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return FrameworkComplianceRunnerStepConfig

    @classmethod
    def extract_component_config(
        cls, config: FrameworkComplianceRunnerStepConfig
    ) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "probe_timeout_seconds": config.probe_timeout_seconds,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: FrameworkComplianceRunnerStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._probe_timeout: float = float(component_config["probe_timeout_seconds"])

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"FrameworkComplianceRunnerStep {self.name!r}: input_data must "
                f"be a dict, got {type(input_data).__name__}"
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
                f"FrameworkComplianceRunnerStep {self.name!r}: input_data['code_source'] "
                f"must be a non-empty string"
            )

        # 1. AST-parse to find framework-class subclasses to probe.
        target_classes = self._find_target_classes(candidate)
        if not target_classes:
            # No framework class to probe. Pass straight through; the
            # candidate may be a free-form function (MBPP-style) that
            # this scaffold has no opinion on.
            log.info(
                "FrameworkComplianceRunnerStep %r: no framework classes found; pass-through",
                self.name,
            )
            return self._make_output(
                decision="pass",
                critique="PASS",
                candidate=candidate,
                input_data=input_data,
            )

        # 2. Build + run the probe in subprocess. Script delivery via
        # stdin (not ``-c``) to avoid the macOS ARG_MAX cap (~256 KB)
        # — same fix as ``tests/benchmarks/sandbox.py``.
        import subprocess  # noqa: PLC0415
        import sys as _sys  # noqa: PLC0415

        probe_script_body = _PROBE_SCRIPT_TEMPLATE.format(target_classes=repr(target_classes))
        # Concatenate candidate code + probe; same shape as the
        # benchmark sandbox so the candidate's classes are visible
        # in ``__main__``.
        full_script = (
            "import sys, traceback\n"
            "try:\n"
            + _indent(candidate, 4)
            + "\n"
            + _indent(probe_script_body, 4)
            + "\n"
            + "except BaseException:\n"
            + "    traceback.print_exc()\n"
            + "    sys.exit(1)\n"
            + "sys.exit(0)\n"
        )

        try:
            completed = subprocess.run(
                [_sys.executable, "-"],
                input=full_script,
                capture_output=True,
                text=True,
                timeout=self._probe_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._make_output(
                decision="fix",
                critique=(
                    f"Compliance probe timed out after {self._probe_timeout}s. "
                    "The candidate's class loading or process() invocation is "
                    "hung or extremely slow. Likely cause: an infinite loop in "
                    "process(), or a heavy import chain. Simplify the code."
                ),
                candidate=candidate,
                input_data=input_data,
            )

        # 3. Parse the probe report.
        report = self._extract_report(completed.stdout)
        if report is None:
            # Probe died before emitting the report — e.g., import error
            # at module load. Surface stderr as critique.
            critique = (
                "Compliance probe could not load the module. Likely cause: "
                "import error or syntax error. Stderr tail:\n"
                f"{(completed.stderr or '')[-400:] or '<empty>'}"
            )
            return self._make_output(
                decision="fix",
                critique=critique,
                candidate=candidate,
                input_data=input_data,
            )

        failures = report.get("failures", [])
        if not failures:
            return self._make_output(
                decision="pass",
                critique="PASS",
                candidate=candidate,
                input_data=input_data,
            )

        critique_lines: list[str] = []
        for f in failures:
            critique_lines.append(
                f"- Class ``{f['class']}`` failed at stage ``{f['stage']}``: {f['error']}"
            )

        return self._make_output(
            decision="fix",
            critique="\n".join(critique_lines),
            candidate=candidate,
            input_data=input_data,
        )

    def _find_target_classes(self, code: str) -> list[tuple[str, str]]:
        """Return list of (class_name, base_module_key) tuples.

        Only includes classes inheriting BaseStep / ToolBase /
        Workflow / BaseAgent (matched by simple Name lookup).
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        out: list[tuple[str, str]] = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                base_name = None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name in _FRAMEWORK_BASES_TO_MODULE:
                    out.append((node.name, _FRAMEWORK_BASES_TO_MODULE[base_name]))
                    break
        return out

    @staticmethod
    def _extract_report(stdout: str) -> dict[str, Any] | None:
        """Pull the JSON report out of the probe's stdout."""
        marker = "===COMPLIANCE_REPORT==="
        if marker not in stdout:
            return None
        # The line after the marker is the JSON.
        m = re.search(re.escape(marker) + r"\s*\n(.+?)(?:\n|\Z)", stdout, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _make_output(
        *,
        decision: str,
        critique: str,
        candidate: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "decision": decision,
            "code_source": candidate,
            "previous_attempt": candidate,
            "critique": critique,
            "code_spec": input_data.get("code_spec"),
            "entry_point": input_data.get("entry_point"),
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
        }


__all__ = [
    "FrameworkComplianceRunnerStep",
    "FrameworkComplianceRunnerStepConfig",
]
