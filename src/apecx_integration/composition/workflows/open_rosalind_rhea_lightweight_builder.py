"""Lightweight WorkflowBuilder variant of the open_rosalind_rhea workflow.

Programmatic counterpart to ``open_rosalind_rhea/workflow.yml``. One of
the three legitimate Open-Rosalind-via-Rhea construction paths:

1. Hand-authored YAML  + ``Workflow.from_config(path)``           (canonical)
2. ``WorkflowBuilder`` programmatic API  + ``.load()``             ← THIS FILE
3. Generated per-problem by ``rhea_workflow`` codegen (discovers the
   Rhea tool catalog at generation time via ``RheaMCPDiscovery``)

The topology is identical to the YAML:

    workflow_input  ->  sequence_tool (ToolExecutionStep, backend=rhea)
                    ->  workflow_output

Authoring constraints (same as the structural-consensus lightweight
builder — see ``benchmark_structural_consensus_lightweight_builder.py``):

* The framework's CLOSED-CLASS RULE forbids inline-dict configs for
  Step subclasses. The step config is path-referenced.
* ``WorkflowBuilder.add_link`` emits a FLAT link shape that the
  ``Workflow.from_config`` resolver silently drops; ``_nest_link_configs``
  repairs it before ``builder.load()``.

This builder path is useful when an agent composes the workflow in
Python rather than authoring YAML — e.g., the ``rhea_workflow``
codegen could emit a builder script instead of a YAML file.
"""

from __future__ import annotations

from pathlib import Path

_WORKFLOW_DIR = Path(__file__).resolve().parent / "open_rosalind_rhea"


def build_open_rosalind_rhea_workflow_lightweight():
    """Programmatic builder for the open_rosalind_rhea workflow.

    Returns a loaded ``Workflow`` ready for ``.process()`` +
    ``.wait_for_cascade()``. Loading does NOT require a Rhea worker —
    the RheaAdapter is resolved at ``process()`` time, not load time.
    A RUN requires ``RheaAdapter.from_env()`` (reads ``$RHEA_MCP_URL``)
    to have registered the adapter.

    Raises
    ------
    FileNotFoundError
        If the referenced step config YAML is missing.
    """
    from nanobrain.lightweight import WorkflowBuilder

    step_yml = _WORKFLOW_DIR / "steps" / "sequence_tool.yml"
    if not step_yml.is_file():
        raise FileNotFoundError(f"open_rosalind_rhea_lightweight: missing step config {step_yml}")

    builder = WorkflowBuilder(
        name="open_rosalind_rhea_lightweight",
        description=(
            "Standalone Open-Rosalind-via-Rhea workflow, authored "
            "programmatically. Mirrors open_rosalind_rhea/workflow.yml."
        ),
    )

    builder.add_input("workflow_input", "DataUnitMemory")
    builder.add_output("workflow_output", "DataUnitMemory")

    # ToolExecutionStep — path-reference config (CLOSED-CLASS rule).
    # The framework step self-unwraps the trigger envelope, so no
    # apecx-side subclass is needed.
    builder.add_step(
        "sequence_tool",
        "nanobrain.library.steps.tool_execution_step.ToolExecutionStep",
        config=str(step_yml),
    )

    builder.add_link(
        source="workflow_input",
        target="sequence_tool.sequence_tool_input",
        link_type="direct",
        link_name="input_to_tool",
        auto_transfer=True,
    )
    builder.add_link(
        source="sequence_tool.sequence_tool_output",
        target="workflow_output",
        link_type="direct",
        link_name="tool_to_output",
        auto_transfer=True,
    )

    _nest_link_configs(builder.workflow_config)
    return builder.load()


def _nest_link_configs(wf_config: dict) -> None:
    """Repair the builder's flat-link shape into the framework's nested shape.

    Identical helper to the one in
    ``benchmark_structural_consensus_lightweight_builder.py``. The
    framework's ``WorkflowBuilder.add_link`` emits FLAT link entries;
    ``Workflow.from_config`` expects NESTED (``{class, config: {...}}``)
    or it silently drops the link. See that file's module docstring
    for the full failure-shape analysis.
    """
    links = wf_config.get("links")
    if not isinstance(links, dict):
        return
    for link_name, entry in list(links.items()):
        if not isinstance(entry, dict):
            continue
        if "config" in entry and isinstance(entry["config"], dict):
            continue
        cls = entry.get("class")
        nested = {k: v for k, v in entry.items() if k not in {"name", "class", "description"}}
        nested.setdefault("link_type", "direct")
        wf_config["links"][link_name] = {
            "name": entry.get("name", link_name),
            "class": cls,
            "description": entry.get("description"),
            "config": nested,
        }


__all__ = ["build_open_rosalind_rhea_workflow_lightweight"]
