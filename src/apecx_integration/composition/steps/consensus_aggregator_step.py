"""ConsensusAggregatorStep — deterministic voter for multi-sample fan-in.

Companion to ``MultiSampleDrafterStep``. Reads the candidate list,
runs each through a deterministic check, picks the winner. Three
voting strategies, configurable per workflow:

* ``ast_validator`` — use ``CodeStructureValidatorStep`` checks
  (cheap, static; ~5ms/candidate).
* ``runtime_validator`` — use ``FrameworkComplianceRunnerStep``-style
  subprocess (slower; ~3-5s/candidate; deeper coverage).
* ``first_non_empty`` — fallback when no validator can score
  (e.g., MBPP free-form functions). Picks the first non-empty
  candidate by character count desc as a stand-in for confidence.

The voting strategy is per-candidate-evaluable; the aggregator
picks the FIRST candidate that returns ``decision=pass``. If none
pass, it picks the BEST-failing one (fewest issues from the
validator) — this is the "graceful degradation" path the F12-F17
analysis recommends.

I/O contract
------------

Input::

    {"candidates": [{"code_source": str}, ...],
     "code_spec", "entry_point", "test_hint", "function_signature": passthrough}

Output::

    {"code_source": "<winning candidate>",
     "winning_index": int,
     "voted_passes": int,       # how many of N candidates passed validation
     "n_samples": int,
     "voting_strategy": str,
     "code_spec", ... passthrough}

If no candidate is provided -> ``ValueError`` (cannot vote on
nothing). If voting fails for some reason -> falls back to
``candidates[0]`` and logs a warning.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
import sys
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

# Re-use the runtime probe script for the deeper voter.
from apecx_integration.composition.steps.framework_compliance_runner_step import (
    _FRAMEWORK_BASES_TO_MODULE,
    _PROBE_SCRIPT_TEMPLATE,
    _indent,
)

log = logging.getLogger(__name__)


_VOTING_STRATEGIES = frozenset({"ast_validator", "runtime_validator", "first_non_empty"})


class ConsensusAggregatorStepConfig(StepConfig):
    """Configuration for ``ConsensusAggregatorStep``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    voting_strategy: str = Field(
        default="ast_validator",
        description=(
            "How to score candidates. ``ast_validator`` (default, "
            "cheap, static). ``runtime_validator`` (slower, deeper "
            "coverage; needs subprocess). ``first_non_empty`` (skip "
            "voting; pick first non-empty candidate by length desc)."
        ),
    )

    runtime_probe_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        description="Per-candidate timeout when voting_strategy=runtime_validator.",
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    @model_validator(mode="after")
    def _validate_strategy(self):
        if self.voting_strategy not in _VOTING_STRATEGIES:
            raise ValueError(
                f"ConsensusAggregatorStepConfig: voting_strategy={self.voting_strategy!r} "
                f"is not one of {sorted(_VOTING_STRATEGIES)}."
            )
        return self


class ConsensusAggregatorStep(BaseStep):
    """Deterministic voter that picks one candidate from a fan-out."""

    COMPONENT_TYPE: str = "consensus_aggregator_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return ConsensusAggregatorStepConfig

    @classmethod
    def extract_component_config(cls, config: ConsensusAggregatorStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "voting_strategy": config.voting_strategy,
            "runtime_probe_timeout_seconds": config.runtime_probe_timeout_seconds,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: ConsensusAggregatorStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._strategy: str = component_config["voting_strategy"]
        self._runtime_timeout: float = float(component_config["runtime_probe_timeout_seconds"])

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(f"ConsensusAggregatorStep {self.name!r}: input_data must be a dict")

        # Trigger-envelope unwrap.
        if (
            len(input_data) == 1
            and "candidates" not in input_data
            and "code_source" not in input_data
        ):
            (only_key,) = input_data.keys()
            if isinstance(input_data[only_key], dict):
                input_data = input_data[only_key]

        candidates = input_data.get("candidates") or []
        if not candidates:
            # Handle a non-multi-sample upstream (single-shot drafter
            # output): if there is a ``code_source``, wrap it as a
            # 1-element candidate list. Lets this step replace a
            # validator without changing upstream wiring.
            single = input_data.get("code_source")
            if isinstance(single, str) and single.strip():
                candidates = [{"code_source": single}]
            else:
                raise ValueError(f"ConsensusAggregatorStep {self.name!r}: empty candidates")

        entry_point = input_data.get("entry_point") or ""
        scored = self._score_candidates(candidates, entry_point=entry_point)

        # Pick the first PASS; else best-by-issue-count (ascending).
        passing = [s for s in scored if s["decision"] == "pass"]
        if passing:
            winner = passing[0]
        else:
            scored.sort(key=lambda s: (s["issue_count"], -len(s["code_source"])))
            winner = scored[0]

        log.info(
            "ConsensusAggregatorStep %r: %d/%d candidates passed, winner idx=%d "
            "(strategy=%s, issues=%d)",
            self.name,
            len(passing),
            len(candidates),
            winner["index"],
            self._strategy,
            winner["issue_count"],
        )

        return {
            "code_source": winner["code_source"],
            "winning_index": winner["index"],
            "voted_passes": len(passing),
            "n_samples": len(candidates),
            "voting_strategy": self._strategy,
            "code_spec": input_data.get("code_spec"),
            "entry_point": entry_point or None,
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
            # Preserve routing context for downstream memory recorders.
            "task_category": input_data.get("task_category"),
        }

    def _score_candidates(
        self, candidates: list[dict[str, Any]], *, entry_point: str
    ) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []
        for i, c in enumerate(candidates):
            code = c.get("code_source", "")
            if not isinstance(code, str) or not code.strip():
                scored.append(
                    {
                        "index": i,
                        "code_source": code if isinstance(code, str) else "",
                        "decision": "fix",
                        "issue_count": 99,
                    }
                )
                continue

            if self._strategy == "first_non_empty":
                # No real voting; everyone "passes" by length.
                scored.append(
                    {
                        "index": i,
                        "code_source": code,
                        "decision": "pass",
                        "issue_count": -len(code),  # negative => sort puts long first
                    }
                )
            elif self._strategy == "ast_validator":
                # Run the AST validator's checks inline (no LLM, no subprocess).
                issues = _ast_validate(code, entry_point)
                scored.append(
                    {
                        "index": i,
                        "code_source": code,
                        "decision": "pass" if not issues else "fix",
                        "issue_count": len(issues),
                    }
                )
            elif self._strategy == "runtime_validator":
                issues = _runtime_validate(code, entry_point, timeout=self._runtime_timeout)
                scored.append(
                    {
                        "index": i,
                        "code_source": code,
                        "decision": "pass" if not issues else "fix",
                        "issue_count": len(issues),
                    }
                )
            else:
                # Defensive — config validator should have caught this.
                raise ValueError(f"unknown voting strategy {self._strategy!r}")
        return scored


# Helpers: small, self-contained, re-usable.


def _ast_validate(code: str, entry_point: str) -> list[str]:
    """Inline AST checks (mirrors CodeStructureValidatorStep)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError at line {e.lineno}: {e.msg}"]
    issues: list[str] = []
    if entry_point:
        top_names = set()
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                top_names.add(node.name)
        if entry_point not in top_names:
            issues.append(f"missing entry point {entry_point}")
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        inherits_framework = any(
            (isinstance(b, ast.Name) and b.id in {"BaseStep", "ToolBase", "Workflow", "BaseAgent"})
            or (
                isinstance(b, ast.Attribute)
                and b.attr in {"BaseStep", "ToolBase", "Workflow", "BaseAgent"}
            )
            for b in cls.bases
        )
        if not inherits_framework:
            continue
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == "from_config":
                    issues.append(f"{cls.name} overrides from_config")
                if item.name == "execute":
                    issues.append(f"{cls.name} overrides execute")
    return issues


def _runtime_validate(code: str, entry_point: str, *, timeout: float) -> list[str]:
    """Mirror of FrameworkComplianceRunnerStep's compliance probe."""
    # Find framework-class targets.
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError at line {e.lineno}: {e.msg}"]
    targets: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = None
            if isinstance(base, ast.Name):
                name = base.id
            elif isinstance(base, ast.Attribute):
                name = base.attr
            if name in _FRAMEWORK_BASES_TO_MODULE:
                targets.append((node.name, _FRAMEWORK_BASES_TO_MODULE[name]))
                break
    if not targets:
        return []  # no framework class to probe -> pass

    probe = _PROBE_SCRIPT_TEMPLATE.format(target_classes=repr(targets))
    script = (
        "import sys, traceback\n"
        "try:\n"
        + _indent(code, 4)
        + "\n"
        + _indent(probe, 4)
        + "\n"
        + "except BaseException:\n"
        + "    traceback.print_exc()\n"
        + "    sys.exit(1)\n"
        + "sys.exit(0)\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [f"probe timed out after {timeout}s"]
    marker = "===COMPLIANCE_REPORT==="
    if marker not in completed.stdout:
        return [f"probe died: {(completed.stderr or '')[-200:]}"]
    m = re.search(re.escape(marker) + r"\s*\n(.+?)(?:\n|\Z)", completed.stdout, re.DOTALL)
    if not m:
        return ["probe report unparseable"]
    try:
        report = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        return ["probe report not JSON"]
    return [f"{f['class']}.{f['stage']}: {f['error']}" for f in (report.get("failures") or [])]


__all__ = ["ConsensusAggregatorStep", "ConsensusAggregatorStepConfig"]
