"""Lightweight nanobrain builder for the structural-consensus scaffold.

Programmatic counterpart to ``benchmark_structural_consensus/workflow.yml``.
Demonstrates the **WorkflowBuilder** authoring path — one of the three
legitimate ways nanobrain workflows can be constructed:

1. Hand-authored YAML  + ``Workflow.from_config(path)``           (canonical)
2. ``WorkflowBuilder`` programmatic API  + ``.load()``             ← THIS FILE
3. Workflow.from_skeleton + bindings (template-based, G9)

Authoring constraints discovered during build
---------------------------------------------

* **CLOSED-CLASS RULE**: Step subclasses ONLY accept config via file
  path, NEVER inline-dict. The builder's ``add_step(..., config=<dict>)``
  form is therefore reserved for DataUnit/Link/Trigger configs. We
  pass ``config=<path>`` strings for steps, pointing at the same
  YAMLs the canonical ``benchmark_structural_consensus/workflow.yml``
  uses.

* **Builder link-shape gotcha (found while building this file)**:
  the framework's ``WorkflowBuilder.add_link`` emits FLAT link entries
  (``{class, source, target, auto_transfer, ...}`` at top level), but
  the framework's ``Workflow.from_config`` resolver expects NESTED
  link entries (``{class, config: {source, target, link_type, ...}}``)
  so its recursive ConfigBase loader can inflate the inner ``config``
  into a real ``DirectLinkConfig``. The flat shape silently produces
  a workflow whose ``step_links`` is empty — the workflow loads, no
  exception, but no link transfers fire.

  Workaround: after the builder finishes adding links, this helper
  post-processes ``builder.workflow_config["links"]`` into the nested
  shape BEFORE calling ``builder.load()``. This is a known framework
  silent-failure shape worth filing upstream (the builder's
  ``add_link`` should emit nested by default).

Why ship both YAML + builder
----------------------------

YAML is the auditable source of truth (diffable, reviewable, the same
loader runs in production). The builder is how an LLM that's been
asked to compose a workflow programmatically would output it — Python
is easier for the model to author than the nested YAML schema, and
the builder's per-method FAIL-FAST checks (unknown link_type, missing
condition for conditional, etc.) catch typos at compose time.

Brutal-truth note
-----------------

The structural_consensus shape (F18) underperformed F17's single-
drafter winner by -10pp. We ship the lightweight variant anyway
because: (a) the AUTHORING PATTERN is what matters for adoption,
not WIN/LOSS of this particular topology; (b) F18's null result is
itself useful evidence — the builder lets future iterations swap in
prompt-perturbing fan-out (the strong form of SGDe) without rewriting
the wiring on disk.
"""

from __future__ import annotations

from pathlib import Path

_WORKFLOW_DIR = Path(__file__).resolve().parent / "benchmark_structural_consensus"


def build_structural_consensus_workflow_lightweight():
    """Programmatic builder for the structural-consensus scaffold.

    Topology::

        workflow_input
            -> task_router_consensus (TaskCategoryRouterStep)
            -> multi_drafter         (MultiSampleDrafterStep, N=3, T=0.5)
            -> aggregator            (ConsensusAggregatorStep, AST voter)
            -> workflow_output

    Returns
    -------
    Workflow
        A loaded Workflow instance ready for ``.process()`` +
        ``.wait_for_cascade()``. No YAML on disk for the workflow
        itself — the builder uses a tempfile via ``.load()``.

    Raises
    ------
    FileNotFoundError
        If any referenced step config YAML is missing.
    """
    from nanobrain.lightweight import WorkflowBuilder

    # Step config YAMLs — same files the canonical workflow.yml uses.
    # We verify they exist BEFORE building so the failure is loud.
    router_yml = _WORKFLOW_DIR / "steps" / "router.yml"
    multi_drafter_yml = _WORKFLOW_DIR / "steps" / "multi_drafter.yml"
    aggregator_yml = _WORKFLOW_DIR / "steps" / "aggregator.yml"
    for p in (router_yml, multi_drafter_yml, aggregator_yml):
        if not p.is_file():
            raise FileNotFoundError(
                f"benchmark_structural_consensus_lightweight: missing step config {p}"
            )

    builder = WorkflowBuilder(
        name="benchmark_structural_consensus_lightweight",
        description=(
            "SGDe-style fan-out/fan-in scaffold authored programmatically. "
            "Topology mirrors benchmark_structural_consensus/workflow.yml."
        ),
    )

    # Workflow-level ports.
    builder.add_input("workflow_input", "DataUnitMemory")
    builder.add_output("workflow_output", "DataUnitMemory")

    # Steps — path-reference configs (framework's CLOSED-CLASS rule).
    builder.add_step(
        "task_router_consensus",
        "apecx_integration.composition.steps.task_category_router_step.TaskCategoryRouterStep",
        config=str(router_yml),
    )
    builder.add_step(
        "multi_drafter",
        "apecx_integration.composition.steps.multi_sample_drafter_step.MultiSampleDrafterStep",
        config=str(multi_drafter_yml),
    )
    builder.add_step(
        "aggregator",
        "apecx_integration.composition.steps.consensus_aggregator_step.ConsensusAggregatorStep",
        config=str(aggregator_yml),
    )

    # Links — every DirectLink carries auto_transfer=true.
    # Without auto_transfer, the link silently no-ops on trigger
    # (dominant nanobrain silent-failure shape, see G7).
    builder.add_link(
        source="workflow_input",
        target="task_router_consensus.router_input",
        link_type="direct",
        link_name="input_to_router",
        auto_transfer=True,
    )
    builder.add_link(
        source="task_router_consensus.router_output",
        target="multi_drafter.multi_drafter_input",
        link_type="direct",
        link_name="router_to_multi_drafter",
        auto_transfer=True,
    )
    builder.add_link(
        source="multi_drafter.multi_drafter_output",
        target="aggregator.aggregator_input",
        link_type="direct",
        link_name="multi_drafter_to_aggregator",
        auto_transfer=True,
    )
    builder.add_link(
        source="aggregator.aggregator_output",
        target="workflow_output",
        link_type="direct",
        link_name="aggregator_to_output",
        auto_transfer=True,
    )

    _nest_link_configs(builder.workflow_config)
    return builder.load()


def _nest_link_configs(wf_config: dict) -> None:
    """Repair the builder's flat-link shape into the framework's nested shape.

    Mutates in place. The framework's ``Workflow.from_config`` resolver
    inflates link entries by reading their nested ``config:`` block;
    the builder emits a flat shape, which silently fails resolution.
    See module docstring for the full failure shape.
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


__all__ = ["build_structural_consensus_workflow_lightweight"]
