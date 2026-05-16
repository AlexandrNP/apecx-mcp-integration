"""Programmatic construction of the pre-warm workflow.

Mirrors the YAML-based workflow at
``infrastructure/prewarm_workflow/configs/prewarm_workflow.yml`` using
:class:`nanobrain.lightweight.WorkflowBuilder` — the "lightweight"
workflow authoring path. Both paths produce semantically identical
nanobrain Workflows; this module exists to:

* Demonstrate that the pre-warm pipeline is not YAML-load-bearing —
  the same DAG can be assembled at runtime in Python.
* Provide an entry point for callers that need to construct the
  workflow dynamically (e.g., an operator-supplied catalog override
  that changes the install_config schema).
* Cross-validate the YAML path: a unit test imports both, loads both,
  and asserts the resulting Workflows expose the same child_steps +
  step_links + input/output DUs.

The step class implementations and step config YAMLs are SHARED with
the YAML-based workflow — the builder just rewires them. No code
duplication.

Note on `config: <yaml-path>` references
----------------------------------------
The step YAMLs are passed as ABSOLUTE paths to ``add_step()`` because
:meth:`WorkflowBuilder.load` materializes the workflow config to a
temp file in ``/tmp`` and the framework resolves ``config:`` relative
to THAT location. A relative path like ``"collect_tools_step.yml"``
would silently miss the configs in /tmp. Using absolute paths keeps
this invariant explicit + makes failures fail-loud at load time
rather than silently substituting defaults.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from nanobrain.core.workflow import Workflow
from nanobrain.lightweight.workflow_builder import WorkflowBuilder

_CONFIGS_DIR = Path(__file__).resolve().parent / "configs"

# Step class paths — the same ones used by prewarm_workflow.yml's
# ``steps:`` block. Single source of truth so renaming/moving a step
# class breaks both authoring paths together rather than letting them
# drift.
_COLLECT_TOOLS_CLASS = (
    "apecx_integration.infrastructure.prewarm_workflow.collect_tools_step.CollectToolsStep"
)
_INSTALL_TOOLS_CLASS = (
    "apecx_integration.infrastructure.prewarm_workflow.install_tools_step.InstallToolsStep"
)
_AGGREGATE_REPORT_CLASS = (
    "apecx_integration.infrastructure.prewarm_workflow.aggregate_report_step.AggregateReportStep"
)


def build_prewarm_workflow_via_builder():
    """Construct the pre-warm workflow programmatically.

    Returns a fully-loaded :class:`nanobrain.core.workflow.Workflow`
    ready for ``initialize()`` + ``process({"prewarm_request": ...})``.
    Equivalent to ``Workflow.from_config(prewarm_workflow.yml)`` but
    assembled in Python via :class:`WorkflowBuilder`.

    Use this when:

    * You need to construct the pre-warm workflow at runtime from
      values not known at YAML-edit time.
    * You're demonstrating the lightweight authoring path for the
      benefit of LLM-generated code or operator tutorials.

    For all other cases prefer ``Workflow.from_config(yml_path)`` —
    the YAML is reviewable, diff-friendly, and version-controlled.
    """
    builder = WorkflowBuilder(
        name="prewarm_workflow_builder",
        description=(
            "Rhea tool-env pre-warm pipeline assembled programmatically "
            "via WorkflowBuilder. Equivalent to the YAML-based "
            "prewarm_workflow but exercises the lightweight authoring "
            "path."
        ),
    )

    # Workflow-level entry/exit DUs.
    builder.add_input("prewarm_request", data_unit_type="DataUnitMemory")
    builder.add_output("prewarm_report", data_unit_type="DataUnitMemory")

    # Three steps, each backed by its existing per-step YAML config
    # (absolute paths so the temp-file load step resolves them).
    builder.add_step(
        "collect_tools",
        _COLLECT_TOOLS_CLASS,
        config=str(_CONFIGS_DIR / "collect_tools_step.yml"),
    )
    builder.add_step(
        "install_tools",
        _INSTALL_TOOLS_CLASS,
        config=str(_CONFIGS_DIR / "install_tools_step.yml"),
    )
    builder.add_step(
        "aggregate_report",
        _AGGREGATE_REPORT_CLASS,
        config=str(_CONFIGS_DIR / "aggregate_report_step.yml"),
    )

    # Four DirectLinks matching the YAML's link block. ``connect()``
    # generates a DirectLink with the workflow's config_version: 2
    # default of auto_transfer=true (G7 Step 3) — explicit but
    # framework-enforced.
    builder.connect("prewarm_request", "collect_tools.prewarm_request")
    builder.connect(
        "collect_tools.collect_tools_output",
        "install_tools.collect_tools_output",
    )
    builder.connect(
        "install_tools.install_tools_output",
        "aggregate_report.install_tools_output",
    )
    builder.connect("aggregate_report.prewarm_report", "prewarm_report")

    # WORKAROUND for friction-log #26: the lightweight builder emits
    # each link entry in FLAT shape ({class, source, target,
    # auto_transfer}), but the framework's LinkBase.from_config
    # expects NESTED shape ({class, config: {source, target,
    # auto_transfer, ...}}). Without this rewrap the workflow loads
    # with 0 functional links — a silent-failure shape that lets the
    # workflow LOAD but never EXECUTE the cascade. When the framework
    # builder catches up to nested shape, _rewrap_link_entries_nested
    # becomes a no-op.
    cfg = builder.get_config()
    cfg["links"] = _rewrap_link_entries_nested(cfg.get("links") or {})
    return Workflow.from_config(_materialize_config_as_yaml(cfg))


def _rewrap_link_entries_nested(
    links_flat: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Convert flat-shape link entries to nested config-keyed shape."""
    rewrapped: dict[str, dict[str, Any]] = {}
    for name, entry in links_flat.items():
        if not isinstance(entry, dict):
            rewrapped[name] = entry
            continue
        if "config" in entry:
            rewrapped[name] = entry
            continue
        cls = entry.get("class")
        nested_config = {k: v for k, v in entry.items() if k not in ("class", "name")}
        nested_config.setdefault("link_type", "direct")
        rewrapped[name] = {"name": name, "class": cls, "config": nested_config}
    return rewrapped


def _materialize_config_as_yaml(cfg: dict[str, Any]) -> str:
    """Write ``cfg`` to a temp YAML and return the path string.

    The file persists in /tmp until OS cleanup — the loader holds it
    for relative-path resolution during the cascade init, so deleting
    it eagerly would race the workflow's late re-reads.
    """
    fd, path = tempfile.mkstemp(suffix=".yml", prefix="apecx_prewarm_builder_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return path


def builder_workflow_config() -> dict[str, Any]:
    """Return the WorkflowBuilder-generated dict WITHOUT loading.

    Useful for tests / debugging — lets you inspect the config the
    builder produced before paying the workflow-load cost.
    """
    builder = WorkflowBuilder(name="prewarm_workflow_builder")
    builder.add_input("prewarm_request", data_unit_type="DataUnitMemory")
    builder.add_output("prewarm_report", data_unit_type="DataUnitMemory")
    builder.add_step(
        "collect_tools",
        _COLLECT_TOOLS_CLASS,
        config=str(_CONFIGS_DIR / "collect_tools_step.yml"),
    )
    builder.add_step(
        "install_tools",
        _INSTALL_TOOLS_CLASS,
        config=str(_CONFIGS_DIR / "install_tools_step.yml"),
    )
    builder.add_step(
        "aggregate_report",
        _AGGREGATE_REPORT_CLASS,
        config=str(_CONFIGS_DIR / "aggregate_report_step.yml"),
    )
    builder.connect("prewarm_request", "collect_tools.prewarm_request")
    builder.connect(
        "collect_tools.collect_tools_output",
        "install_tools.collect_tools_output",
    )
    builder.connect(
        "install_tools.install_tools_output",
        "aggregate_report.install_tools_output",
    )
    builder.connect("aggregate_report.prewarm_report", "prewarm_report")
    return builder.get_config()


__all__ = ["build_prewarm_workflow_via_builder", "builder_workflow_config"]
