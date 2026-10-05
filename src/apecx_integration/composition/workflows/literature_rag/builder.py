"""literature_rag — lightweight builder (MCP catalog entry-point).

A no-arg callable that constructs the runnable literature_rag workflow with
``nanobrain.lightweight.WorkflowBuilder`` and returns ``builder.load()``.

Why a builder rather than ``literature_rag_workflow.yml`` directly: that YAML is
the pre-stamped-records entry point (its ``workflow_input`` expects
``stamped_records`` already present). A desktop MCP caller only has
``{organism, question}`` — it cannot supply ``stamped_records``. This builder
prepends ONE harvest front-end step (``HarvestStampStep``) that produces those
records, then reuses the EXISTING three-step cascade unchanged:

    workflow_input {organism, question, max_papers?}
      → harvest_stamp  HarvestStampStep   → {organism, question, stamped_records}
      → resolve        ResolveOrganismStep → +target_iri
      → ontology_filter OntologyFilterStep → +filtered_records (kept == target_iri)
      → literature_rag LiteratureRagStep   → {answer, citations, status}
      → workflow_output

Each DirectLink inherits ``auto_transfer: true`` from config_version 2 (the
builder default) — the dominant nanobrain silent-failure (a link that no-ops on
auto_transfer=false) cannot occur. The three reused steps keep their own classes
and behavior; the builder only declares their data units + links (equivalent to
their sibling YAMLs, which stay the source of truth for the
``Workflow.from_config`` path).

LLM mode: ``LiteratureRagStep`` carries ``LLM_ROLE='final_synthesis'`` — in desktop
locus it omits the apecx LLM and hands the host the retrieved evidence, so the MCP
``requires_llm`` gate does NOT refuse this tool on a desktop with no Ollama.
"""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.workflows.literature_rag.steps"


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def _trig(du: str) -> list[dict[str, Any]]:
    return [{"class": _TRIGGER, "data_unit": du}]


def _literature_rag_workflow_builder():
    """Build (but do NOT load) the WorkflowBuilder. Exposed so tests can assert the
    topology (e.g. the first-step input DU == the catalog input_envelope_key)
    without loading the full workflow."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "literature_rag",
        "Answer a question from the PubMed literature for one organism, with taxon "
        "ontology grounding: harvest PubMed, keep only papers about the resolved "
        "organism (drop off-organism false positives), then answer with cited PMIDs.",
    )
    b.add_input("workflow_input", "DataUnitMemory")
    b.add_output("workflow_output", "DataUnitMemory")

    # Entry/deposit step: run_workflow deposits the request under THIS step's input DU
    # (literature_rag_input) = the catalog input_envelope_key.
    b.add_step(
        "harvest_stamp",
        f"{_STEPS}.harvest_stamp_step.HarvestStampStep",
        input_data_units=_du("literature_rag_input"),
        output_data_units=_du("harvest_output"),
        triggers=_trig("literature_rag_input"),
    )
    b.add_step(
        "resolve_organism_step",
        f"{_STEPS}.resolve_step.ResolveOrganismStep",
        input_data_units=_du("resolve_input"),
        output_data_units=_du("resolve_output"),
        triggers=_trig("resolve_input"),
    )
    b.add_step(
        "ontology_filter_step",
        f"{_STEPS}.ontology_filter_step.OntologyFilterStep",
        input_data_units=_du("filter_input"),
        output_data_units=_du("filter_output"),
        triggers=_trig("filter_input"),
    )
    b.add_step(
        "literature_rag_step",
        f"{_STEPS}.literature_rag_step.LiteratureRagStep",
        input_data_units=_du("rag_input"),
        output_data_units=_du("rag_output"),
        triggers=_trig("rag_input"),
    )

    b.add_link("workflow_input", "harvest_stamp.literature_rag_input", link_type="direct")
    b.add_link(
        "harvest_stamp.harvest_output", "resolve_organism_step.resolve_input", link_type="direct"
    )
    b.add_link(
        "resolve_organism_step.resolve_output",
        "ontology_filter_step.filter_input",
        link_type="direct",
    )
    b.add_link(
        "ontology_filter_step.filter_output", "literature_rag_step.rag_input", link_type="direct"
    )
    b.add_link("literature_rag_step.rag_output", "workflow_output", link_type="direct")

    return b


def build_literature_rag_workflow():
    """Construct + load the literature_rag workflow (catalog entry-point)."""
    return _literature_rag_workflow_builder().load()


__all__ = ["build_literature_rag_workflow"]
