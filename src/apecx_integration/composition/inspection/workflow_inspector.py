"""Recursive static inspection of a nanobrain workflow YAML (EO-02).

Resolves a workflow YAML's ``steps:`` / ``links:`` tree WITHOUT instantiating any component —
pure static analysis of the on-disk config. Step ``config:`` file references (relative to the
workflow YAML's directory) are loaded; a step whose config is itself a workflow (has its own
``steps:``) is recursed into, bounded by ``max_depth``.

This is the "static composition visibility" surface (``external_orchestration_design.md`` §4/§9):
the scientist sees which steps + tools + parameters a workflow is configured to run, recursively,
grounded in the YAML rather than runtime state.

Loud by design: a missing step-config file raises ``FileNotFoundError`` (a dangling reference is
a real defect, not something to skip silently); a non-mapping YAML raises ``ValueError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


class LinkInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    class_path: str
    source: str
    target: str
    condition: dict[str, Any] | None = None


class StepInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    class_path: str
    config_path: str | None = None
    input_data_units: list[str] = []
    output_data_units: list[str] = []
    nested_workflow: WorkflowInspection | None = None


class WorkflowInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    config_version: int | None = None
    input_data_units: list[str] = []
    output_data_units: list[str] = []
    steps: list[StepInspection] = []
    links: list[LinkInspection] = []
    truncated: bool = False
    """True when ``max_depth`` was hit and a nested workflow was left unexpanded."""


def _du_names(section: Any) -> list[str]:
    return sorted(section.keys()) if isinstance(section, dict) else []


def _resolve_step_config(
    step_cfg: dict[str, Any], base_dir: Path
) -> tuple[str | None, dict[str, Any] | None]:
    """Return ``(relative_config_path, loaded_dict)`` for a step's ``config:``.

    A string config is a file path resolved relative to the workflow YAML's directory; a
    missing file raises (loud — a dangling reference is a defect). A dict config is inline.
    """
    cfg = step_cfg.get("config")
    if cfg is None:
        return None, None
    if isinstance(cfg, str):
        cfg_path = (base_dir / cfg).resolve()
        if not cfg_path.is_file():
            raise FileNotFoundError(
                f"inspect_workflow: step config {cfg_path} (referenced as {cfg!r}) "
                f"does not exist — dangling reference."
            )
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return cfg, (loaded if isinstance(loaded, dict) else None)
    if isinstance(cfg, dict):
        return None, cfg
    return None, None


def _inspect_dict(
    raw: dict[str, Any], base_dir: Path, depth: int, max_depth: int
) -> WorkflowInspection:
    steps_out: list[StepInspection] = []
    truncated = False

    for step_name, step_cfg in (raw.get("steps") or {}).items():
        if not isinstance(step_cfg, dict):
            continue
        config_path, loaded = _resolve_step_config(step_cfg, base_dir)
        nested: WorkflowInspection | None = None
        in_dus: list[str] = []
        out_dus: list[str] = []
        if loaded is not None:
            in_dus = _du_names(loaded.get("input_data_units"))
            out_dus = _du_names(loaded.get("output_data_units"))
            if "steps" in loaded:  # the step config is itself a (sub)workflow
                if depth + 1 <= max_depth:
                    nested_base = (
                        (base_dir / config_path).resolve().parent if config_path else base_dir
                    )
                    nested = _inspect_dict(loaded, nested_base, depth + 1, max_depth)
                else:
                    truncated = True
        steps_out.append(
            StepInspection(
                name=str(step_name),
                class_path=str(step_cfg.get("class", "?")),
                config_path=config_path,
                input_data_units=in_dus,
                output_data_units=out_dus,
                nested_workflow=nested,
            )
        )

    links_out: list[LinkInspection] = []
    for link_name, link_cfg in (raw.get("links") or {}).items():
        if not isinstance(link_cfg, dict):
            continue
        lc = link_cfg.get("config")
        lc = lc if isinstance(lc, dict) else {}
        links_out.append(
            LinkInspection(
                name=str(link_name),
                class_path=str(link_cfg.get("class", "?")),
                source=str(lc.get("source", "?")),
                target=str(lc.get("target", "?")),
                condition=lc.get("condition") if isinstance(lc.get("condition"), dict) else None,
            )
        )

    return WorkflowInspection(
        name=str(raw.get("name", "?")),
        description=raw.get("description"),
        config_version=raw.get("config_version"),
        input_data_units=_du_names(raw.get("input_data_units")),
        output_data_units=_du_names(raw.get("output_data_units")),
        steps=steps_out,
        links=links_out,
        truncated=truncated,
    )


def inspect_workflow(workflow_yaml: str | Path, *, max_depth: int = 3) -> WorkflowInspection:
    """Recursively inspect a workflow YAML into a structured tree (static, no instantiation)."""
    path = Path(workflow_yaml).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"inspect_workflow: {path} is not a file")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"inspect_workflow: {path} did not parse to a mapping (got {type(raw).__name__})"
        )
    return _inspect_dict(raw, base_dir=path.parent, depth=0, max_depth=max_depth)


StepInspection.model_rebuild()
WorkflowInspection.model_rebuild()
