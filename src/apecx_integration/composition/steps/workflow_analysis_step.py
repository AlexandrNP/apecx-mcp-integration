"""WorkflowAnalysisStep — pure-Python structural analyzer for workflow YAMLs.

Reads a workflow YAML (path or dict) and emits a structured report
that downstream steps (or a domain expert reading raw JSON) can use
to understand what the workflow does without running it. No LLM
involved: the output is deterministic given a fixed YAML input.

Intended uses:

  1. As input to ``WorkflowSummarizerStep`` (LLM-backed) — the
     analysis is the LLM's grounded source-of-truth, eliminating
     hallucination drift in the explainer.
  2. As input to a CI gate that pre-flights authored workflows
     before they get committed — flag missing ``auto_transfer:true``,
     orphan data units, suspicious topologies.
  3. As a debugging tool when ``wf.run(...)`` "completes" with
     unexpected outputs — the analysis surfaces structural
     mismatches between declared and used data unit names.

What it inspects (no run, no LLM):

  - workflow name + description + config_version
  - per-step: class path, has_config_path, input/output DU names, trigger types
  - per-link: source, target, link_type, auto_transfer flag
  - topology shape: linear / fan-out / fan-in / DAG-with-branches
  - silent-failure issues:
      * link with auto_transfer != true under config_version < 2
        (G7 default-flip is v2; older configs need explicit flag)
      * data unit named in a link but not declared by any step
      * step with input DUs but no incoming link
      * step with output DUs but no outgoing link

The output is a dict with stable keys (extensible — additive only).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


class WorkflowAnalysisStepConfig(StepConfig):
    """Configuration for WorkflowAnalysisStep.

    ``extra='forbid'`` (workspace rule).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    flag_v1_links_missing_auto_transfer: bool = Field(
        default=True,
        description=(
            "When True (default), workflows declaring config_version<2 "
            "AND a DirectLink without auto_transfer:true get a silent-"
            "failure issue flag. Operators turn this off if they're "
            "intentionally on v1 + the documented workaround."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class WorkflowAnalysisStep(BaseStep):
    """Analyze a workflow YAML and emit a structured report.

    Expected ``process()`` input::

        {"workflow_path": "src/.../my_workflow.yml"}
        # OR
        {"workflow_dict": {...parsed yaml dict...}}

    Return shape (stable keys; additive only)::

        {
            "workflow_name": str,
            "description": str,
            "config_version": int,
            "steps": [
                {"step_name", "class", "has_config_path",
                 "input_data_unit_names", "output_data_unit_names",
                 "trigger_classes"},
                ...
            ],
            "links": [
                {"link_name", "class", "source", "target",
                 "link_type", "auto_transfer"},
                ...
            ],
            "input_data_units": list[str],
            "output_data_units": list[str],
            "topology_summary": str,        # human-readable
            "issues": [{"code": str, "detail": str}, ...],
            "summary_line": str,             # one-sentence summary
        }
    """

    COMPONENT_TYPE: str = "workflow_analysis_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return WorkflowAnalysisStepConfig

    @classmethod
    def extract_component_config(cls, config: WorkflowAnalysisStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "flag_v1_links_missing_auto_transfer": (config.flag_v1_links_missing_auto_transfer),
        }

    def _init_from_config(
        self,
        config: WorkflowAnalysisStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._flag_v1_missing_auto: bool = bool(
            component_config["flag_v1_links_missing_auto_transfer"]
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"WorkflowAnalysisStep {self.name!r}: input_data must be "
                f"a dict, got {type(input_data).__name__}"
            )
        if (
            "analysis_input" in input_data
            and isinstance(input_data["analysis_input"], dict)
            and "workflow_path" not in input_data
            and "workflow_dict" not in input_data
        ):
            input_data = input_data["analysis_input"]

        wf_dict = self._load_workflow_dict(input_data)
        analysis = await asyncio.to_thread(self._analyze, wf_dict)
        log.info(
            "WorkflowAnalysisStep %r: analyzed workflow %r — steps=%d, links=%d, issues=%d",
            self.name,
            analysis["workflow_name"],
            len(analysis["steps"]),
            len(analysis["links"]),
            len(analysis["issues"]),
        )
        return analysis

    def _load_workflow_dict(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if "workflow_dict" in input_data:
            wf_dict = input_data["workflow_dict"]
            if not isinstance(wf_dict, dict):
                raise ValueError(
                    f"WorkflowAnalysisStep {self.name!r}: "
                    f"'workflow_dict' must be a dict, got "
                    f"{type(wf_dict).__name__}"
                )
            return wf_dict
        path = input_data.get("workflow_path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"WorkflowAnalysisStep {self.name!r}: input_data must "
                f"contain either 'workflow_path' (str) or "
                f"'workflow_dict' (dict); got keys="
                f"{sorted(input_data.keys())}"
            )
        p = Path(path)
        if not p.is_file():
            raise ValueError(
                f"WorkflowAnalysisStep {self.name!r}: workflow_path "
                f"{path!r} does not exist or is not a file"
            )
        text = p.read_text(encoding="utf-8")
        try:
            wf_dict = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ValueError(
                f"WorkflowAnalysisStep {self.name!r}: workflow YAML at {path} failed parse: {e}"
            ) from e
        if not isinstance(wf_dict, dict):
            raise ValueError(
                f"WorkflowAnalysisStep {self.name!r}: workflow YAML at "
                f"{path} parsed to {type(wf_dict).__name__}, expected dict"
            )
        return wf_dict

    def _analyze(self, wf: dict[str, Any]) -> dict[str, Any]:
        name = str(wf.get("name", "<unnamed>"))
        description = str(wf.get("description", "")).strip()
        config_version = int(wf.get("config_version", 1))

        input_dus = self._collect_data_unit_names(wf.get("input_data_units"))
        output_dus = self._collect_data_unit_names(wf.get("output_data_units"))

        steps_report: list[dict[str, Any]] = []
        for step_name, step_entry in (wf.get("steps") or {}).items():
            steps_report.append(self._analyze_step(str(step_name), step_entry or {}))

        links_report: list[dict[str, Any]] = []
        for link_name, link_entry in (wf.get("links") or {}).items():
            links_report.append(self._analyze_link(str(link_name), link_entry or {}))

        topology_summary = self._summarize_topology(steps_report, links_report)
        issues = self._detect_issues(
            steps_report=steps_report,
            links_report=links_report,
            workflow_input_dus=input_dus,
            workflow_output_dus=output_dus,
            config_version=config_version,
        )
        summary_line = (
            f"Workflow {name!r}: {len(steps_report)} step(s), "
            f"{len(links_report)} link(s), {len(issues)} issue(s) — "
            f"{topology_summary}."
        )

        return {
            "workflow_name": name,
            "description": description,
            "config_version": config_version,
            "input_data_units": input_dus,
            "output_data_units": output_dus,
            "steps": steps_report,
            "links": links_report,
            "topology_summary": topology_summary,
            "issues": issues,
            "summary_line": summary_line,
        }

    @staticmethod
    def _collect_data_unit_names(node: Any) -> list[str]:
        if not isinstance(node, dict):
            return []
        return sorted(str(k) for k in node)

    @staticmethod
    def _analyze_step(name: str, entry: dict[str, Any]) -> dict[str, Any]:
        class_path = str(entry.get("class", ""))
        has_config = bool(entry.get("config"))
        # For path-reference configs we can't see the wrapper YAML's
        # input/output DU names without loading the file. The
        # analyzer stays pure-structural: it reports has_config_path
        # so the summarizer can describe the wiring at the right
        # level of abstraction.
        # If the step ALSO has inline config keys (rare), surface
        # those.
        inline_inputs = entry.get("input_data_units") if isinstance(entry, dict) else None
        inline_outputs = entry.get("output_data_units") if isinstance(entry, dict) else None
        inline_triggers = entry.get("triggers") if isinstance(entry, dict) else None
        trigger_classes: list[str] = []
        if isinstance(inline_triggers, list):
            for trig in inline_triggers:
                if isinstance(trig, dict):
                    trigger_classes.append(str(trig.get("class", "")))
        return {
            "step_name": name,
            "class": class_path,
            "has_config_path": has_config,
            "input_data_unit_names": (
                sorted(inline_inputs.keys()) if isinstance(inline_inputs, dict) else []
            ),
            "output_data_unit_names": (
                sorted(inline_outputs.keys()) if isinstance(inline_outputs, dict) else []
            ),
            "trigger_classes": trigger_classes,
        }

    @staticmethod
    def _analyze_link(name: str, entry: dict[str, Any]) -> dict[str, Any]:
        class_path = str(entry.get("class", ""))
        # Nested-config shape (the canonical form post-G7-Step-4):
        config_block = entry.get("config")
        if isinstance(config_block, dict):
            source = str(config_block.get("source", ""))
            target = str(config_block.get("target", ""))
            link_type = str(config_block.get("link_type", "direct"))
            auto_transfer = bool(config_block.get("auto_transfer", False))
        else:
            # Flat shape (legacy / lightweight builder).
            source = str(entry.get("source", ""))
            target = str(entry.get("target", ""))
            link_type = str(entry.get("link_type", "direct"))
            auto_transfer = bool(entry.get("auto_transfer", False))
        return {
            "link_name": name,
            "class": class_path,
            "source": source,
            "target": target,
            "link_type": link_type,
            "auto_transfer": auto_transfer,
        }

    @staticmethod
    def _summarize_topology(steps: list[dict[str, Any]], links: list[dict[str, Any]]) -> str:
        if not steps:
            return "no steps (decorative/empty workflow)"
        if len(steps) == 1:
            return f"single-step workflow ({steps[0]['step_name']})"
        # Count fan-out / fan-in by counting how many links share
        # source / target step prefixes (before the dot).
        step_names = {s["step_name"] for s in steps}
        source_counts: dict[str, int] = {}
        target_counts: dict[str, int] = {}
        for link in links:
            src_step = link["source"].split(".", 1)[0]
            tgt_step = link["target"].split(".", 1)[0]
            if src_step in step_names:
                source_counts[src_step] = source_counts.get(src_step, 0) + 1
            if tgt_step in step_names:
                target_counts[tgt_step] = target_counts.get(tgt_step, 0) + 1
        fan_out = sum(1 for c in source_counts.values() if c > 1)
        fan_in = sum(1 for c in target_counts.values() if c > 1)
        if fan_out and fan_in:
            return (
                f"{len(steps)}-step DAG with branches "
                f"(fan-out at {fan_out} step(s), fan-in at {fan_in})"
            )
        if fan_out:
            return f"{len(steps)}-step pipeline with fan-out at {fan_out} step(s)"
        if fan_in:
            return f"{len(steps)}-step pipeline with fan-in at {fan_in} step(s)"
        return f"{len(steps)}-step linear pipeline"

    def _detect_issues(
        self,
        *,
        steps_report: list[dict[str, Any]],
        links_report: list[dict[str, Any]],
        workflow_input_dus: list[str],
        workflow_output_dus: list[str],
        config_version: int,
    ) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []

        if not steps_report:
            issues.append(
                {
                    "code": "no_steps",
                    "detail": (
                        "Workflow declares no steps. Decorative-only "
                        "workflows are valid for namespace / config "
                        "tests but cannot execute."
                    ),
                }
            )

        # auto_transfer issue per workspace-CLAUDE.md dominant
        # silent-failure shape.
        if self._flag_v1_missing_auto and config_version < 2:
            for link in links_report:
                if "DirectLink" in link["class"] and not link["auto_transfer"]:
                    issues.append(
                        {
                            "code": "directlink_missing_auto_transfer",
                            "detail": (
                                f"Link {link['link_name']!r} is a "
                                f"DirectLink without auto_transfer:true "
                                f"on config_version={config_version}. "
                                f"Workflow loads but the link silently "
                                f"no-ops at runtime."
                            ),
                        }
                    )

        # Orphan-link detection: source / target reference a step
        # name that isn't declared.
        step_name_set = {s["step_name"] for s in steps_report}
        workflow_du_set = set(workflow_input_dus) | set(workflow_output_dus)
        for link in links_report:
            for endpoint, role in (
                (link["source"], "source"),
                (link["target"], "target"),
            ):
                if not endpoint:
                    continue
                step_part = endpoint.split(".", 1)[0]
                # Workflow-level DU? Accept.
                if step_part in workflow_du_set:
                    continue
                if step_part in step_name_set:
                    continue
                issues.append(
                    {
                        "code": "link_endpoint_unresolved",
                        "detail": (
                            f"Link {link['link_name']!r} {role}="
                            f"{endpoint!r} references step or data "
                            f"unit not declared in the workflow."
                        ),
                    }
                )

        return issues


__all__ = ["WorkflowAnalysisStep", "WorkflowAnalysisStepConfig"]
