"""harmonized_index_search — a ONE-step inner workflow: run HarmonizedSearchExecuteStep
on a single resolved plan (which carries its ``index``).

This is the inner workflow that ``MapSubworkflowStep`` fans out over the 9 destination
indices in the viral_epitope_analysis resolution leg: the map gives each item a plan
with ``index`` set to one destination index, and this workflow runs the raw + harmonized
Globus queries for that index. No-arg builder (the SubworkflowStep/MapSubworkflowStep
``inner_workflow_builder`` seam expects a no-arg callable returning a loaded Workflow).

The step's input DU is ``plan`` (the resolved plan dict, incl. ``index``) and its output
key is ``envelope_input`` ({markdown, data:{kind:bundle, parts:{...}}}). The map's drive
routes each per-item plan to ``plan`` and collects the workflow-level output.
"""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.steps"


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def build_harmonized_index_search_workflow():
    """Build + load the 1-step harmonized-search-for-one-index inner workflow."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "harmonized_index_search",
        "Run the raw + harmonized Globus queries for ONE destination index from a resolved plan.",
    )
    b.add_input("search_in", "DataUnitMemory")
    b.add_output("search_out", "DataUnitMemory")
    b.add_step(
        "search",
        f"{_STEPS}.harmonized_search_execute_step.HarmonizedSearchExecuteStep",
        input_data_units=_du("plan"),
        output_data_units=_du("envelope_input"),
        triggers=[{"class": _TRIGGER, "data_unit": "plan"}],
    )
    b.add_link("search_in", "search.plan", link_type="direct")
    b.add_link("search.envelope_input", "search_out", link_type="direct")
    return b.load()
