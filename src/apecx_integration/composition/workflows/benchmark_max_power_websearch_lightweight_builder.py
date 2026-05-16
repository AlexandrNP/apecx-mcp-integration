"""Lightweight WorkflowBuilder variant of benchmark_max_power_websearch.

Programmatic counterpart to ``benchmark_max_power_websearch/workflow.yml``.
One of the three legitimate construction paths for this composition:

1. Hand-authored YAML + ``Workflow.from_config(path)``       (canonical)
2. ``WorkflowBuilder`` programmatic API + ``.load()``         ← THIS FILE
3. (no per-problem codegen variant — this is a fixed composition,
   not a generated-per-problem workflow)

The topology is identical to the YAML:

    workflow_input
      -> task_router_max_power (TaskCategoryRouterStep)
      -> memory_reader         (SolutionMemoryStep: similarity_read)
      -> web_search_context    (WebSearchContextStep)
      -> perturbing_drafter    (PromptPerturbingDrafterStep)
      -> aggregator            (ConsensusAggregatorStep)
      -> {workflow_output, memory_recorder -> workflow_recorder_status}

Authoring constraints (same as the other lightweight builders in this
package):

* The framework's CLOSED-CLASS RULE forbids inline-dict configs for
  Step subclasses — every step config is path-referenced (to the
  shared ``benchmark_max_power_websearch/steps/*.yml`` files).
* ``WorkflowBuilder.add_link`` emits a FLAT link shape that
  ``Workflow.from_config`` silently drops; ``_nest_link_configs``
  repairs it before ``builder.load()``.

This path is useful when an agent composes the workflow in Python
rather than authoring YAML.
"""

from __future__ import annotations

from pathlib import Path

_WORKFLOW_DIR = Path(__file__).resolve().parent / "benchmark_max_power_websearch"
_STEPS_DIR = _WORKFLOW_DIR / "steps"

# (step_id, class_path, step_config_filename)
_STEPS: tuple[tuple[str, str, str], ...] = (
    (
        "task_router_max_power",
        "apecx_integration.composition.steps.task_category_router_step.TaskCategoryRouterStep",
        "router.yml",
    ),
    (
        "memory_reader",
        "apecx_integration.composition.steps.solution_memory_step.SolutionMemoryStep",
        "memory_reader.yml",
    ),
    (
        "web_search_context",
        "apecx_integration.composition.steps.web_search_context_step.WebSearchContextStep",
        "web_search_context.yml",
    ),
    (
        "perturbing_drafter",
        "apecx_integration.composition.steps.prompt_perturbing_drafter_step.PromptPerturbingDrafterStep",
        "perturbing_drafter.yml",
    ),
    (
        "aggregator",
        "apecx_integration.composition.steps.consensus_aggregator_step.ConsensusAggregatorStep",
        "aggregator.yml",
    ),
    (
        "memory_recorder",
        "apecx_integration.composition.steps.solution_memory_step.SolutionMemoryStep",
        "memory_recorder.yml",
    ),
)

# (link_name, source, target)
_LINKS: tuple[tuple[str, str, str], ...] = (
    ("input_to_router", "workflow_input", "task_router_max_power.router_input"),
    (
        "router_to_memory_reader",
        "task_router_max_power.router_output",
        "memory_reader.memory_reader_input",
    ),
    (
        "memory_reader_to_web_search",
        "memory_reader.memory_reader_output",
        "web_search_context.web_search_context_input",
    ),
    (
        "web_search_to_drafter",
        "web_search_context.web_search_context_output",
        "perturbing_drafter.perturbing_drafter_input",
    ),
    (
        "drafter_to_aggregator",
        "perturbing_drafter.perturbing_drafter_output",
        "aggregator.aggregator_input",
    ),
    (
        "aggregator_to_memory_recorder",
        "aggregator.aggregator_output",
        "memory_recorder.memory_recorder_input",
    ),
    ("aggregator_to_output", "aggregator.aggregator_output", "workflow_output"),
    (
        "memory_recorder_to_status",
        "memory_recorder.memory_recorder_output",
        "workflow_recorder_status",
    ),
)


def build_max_power_websearch_workflow_lightweight():
    """Programmatic builder for benchmark_max_power_websearch.

    Returns a loaded ``Workflow`` ready for ``.process()`` +
    ``.wait_for_cascade()``. Mirrors
    ``benchmark_max_power_websearch/workflow.yml``.

    Raises
    ------
    FileNotFoundError
        If any referenced step config YAML is missing.
    """
    from nanobrain.lightweight import WorkflowBuilder

    for _, _, fname in _STEPS:
        step_yml = _STEPS_DIR / fname
        if not step_yml.is_file():
            raise FileNotFoundError(
                f"benchmark_max_power_websearch_lightweight: missing step config {step_yml}"
            )

    builder = WorkflowBuilder(
        name="benchmark_max_power_websearch_lightweight",
        description=(
            "Max-power + web-search context composition, authored "
            "programmatically. Mirrors benchmark_max_power_websearch/"
            "workflow.yml."
        ),
    )

    builder.add_input("workflow_input", "DataUnitMemory")
    builder.add_output("workflow_output", "DataUnitMemory")
    builder.add_output("workflow_recorder_status", "DataUnitMemory")

    for step_id, class_path, fname in _STEPS:
        builder.add_step(step_id, class_path, config=str(_STEPS_DIR / fname))

    for link_name, source, target in _LINKS:
        builder.add_link(
            source=source,
            target=target,
            link_type="direct",
            link_name=link_name,
            auto_transfer=True,
        )

    _nest_link_configs(builder.workflow_config)
    return builder.load()


def _nest_link_configs(wf_config: dict) -> None:
    """Repair the builder's flat-link shape into the framework's nested shape.

    ``WorkflowBuilder.add_link`` emits FLAT link entries;
    ``Workflow.from_config`` expects NESTED (``{class, config: {...}}``)
    or it silently drops the link. Identical helper to the one in the
    other lightweight builders in this package.
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


__all__ = ["build_max_power_websearch_workflow_lightweight"]
