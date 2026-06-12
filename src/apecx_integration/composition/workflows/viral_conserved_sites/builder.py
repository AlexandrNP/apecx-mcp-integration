"""viral_conserved_sites — the conserved-sites workflow, built the LIGHTWEIGHT way (EO-53).

This is the catalog entry-point: a no-arg callable that constructs the workflow with the
``nanobrain.lightweight.WorkflowBuilder`` and returns ``builder.load()``. Registered in
``mcp_workflow_catalog.yml`` as a ``kind: lightweight`` source, so it's discoverable via
``list_workflows`` and runnable via ``run_workflow("viral_conserved_sites", {...})``.

Pipeline (each DirectLink inherits ``auto_transfer: true`` from config_version 2; each step
reads the one key it needs out of the prior step's output dict — no TransformLink):

    workflow_input {taxon_id, protein, feature_type?}
      → fetch     BvbrcProteinFastaStep   → {fasta_text, ...}
      → align     LocalMafftAlignStep     → {alignment_fasta, ...}
      → conserve  ConservationScoreStep   → conservation result
      → report    ConservationReportStep  → {markdown, data}
      → envelope  EnvelopeStep            → WorkflowResult
      → workflow_output

We use a local-MAFFT aligner (the lightweight path verified end-to-end); the Rhea/Galaxy
alignment subworkflow is the substitutable heavy path (design §8). The caller resolves a
virus name to an NCBI ``taxon_id`` first (e.g. via harmonized_search) — keeping ambiguity
resolution (and its HITL gate) OUT of this deterministic cascade.
"""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.steps"


# RoC-2a — the authoritative input contract, declared ON the workflow (G6 step_input_schema).
# It is the WRAPPED shape G6 validates at the wire boundary: the entry data unit `fetch_in` holds
# the parameter dict {taxon_id, protein, feature_type?}. The framework FAIL-FASTs at runtime on a
# malformed payload; RoC-2b derives the required params by unwrapping the `fetch_in` level. This is
# the single source of truth (no catalog/workflow drift). `obtain_via` is an annotation for the
# frontier LLM (jsonschema ignores unknown keywords).
FETCH_INPUT_SCHEMA: dict[str, Any] = {
    "json_schema": {
        "type": "object",
        "properties": {
            "fetch_in": {
                "type": "object",
                "properties": {
                    "taxon_id": {
                        "type": "integer",
                        "obtain_via": "resolve the virus name to an NCBI taxon_id via harmonized_search",
                    },
                    "protein": {
                        "type": "string",
                        "obtain_via": "the protein product name/substring (e.g. 'E1', 'capsid')",
                    },
                    "feature_type": {"type": "string"},
                },
                "required": ["taxon_id", "protein"],
            }
        },
        "required": ["fetch_in"],
    }
}


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def _trig(du: str) -> list[dict[str, Any]]:
    return [{"class": _TRIGGER, "data_unit": du}]


def build_viral_conserved_sites_workflow():
    """Construct + load the viral_conserved_sites workflow (catalog lightweight entry-point)."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "viral_conserved_sites",
        "Find conserved protein sites across strains of a virus (BV-BRC → MSA → conservation).",
    )
    b.add_input("workflow_input", "DataUnitMemory")
    b.add_output("workflow_output", "DataUnitMemory")

    b.add_step(
        "fetch",
        f"{_STEPS}.bvbrc_protein_fasta_step.BvbrcProteinFastaStep",
        max_sequences=25,
        # Drop partial CDS records (keep ≥80% of the longest) so the MSA isn't gap-blurred.
        min_length_fraction=0.8,
        # RoC-2a — authoritative input contract (G6, runtime FAIL-FAST + RoC-2b derivation source).
        step_input_schema=FETCH_INPUT_SCHEMA,
        input_data_units=_du("fetch_in"),
        output_data_units=_du("protein_fasta"),
        triggers=_trig("fetch_in"),
    )
    b.add_step(
        "align",
        f"{_STEPS}.local_mafft_align_step.LocalMafftAlignStep",
        input_data_units=_du("align_in"),
        output_data_units=_du("alignment"),
        triggers=_trig("align_in"),
    )
    b.add_step(
        "conserve",
        f"{_STEPS}.conservation_score_step.ConservationScoreStep",
        conservation_threshold=0.9,
        input_data_units=_du("conserve_in"),
        output_data_units=_du("conservation_result"),
        triggers=_trig("conserve_in"),
    )
    b.add_step(
        "report",
        f"{_STEPS}.conservation_report_step.ConservationReportStep",
        input_data_units=_du("report_in"),
        output_data_units=_du("report"),
        triggers=_trig("report_in"),
    )
    b.add_step(
        "envelope",
        f"{_STEPS}.envelope_step.EnvelopeStep",
        input_data_units=_du("envelope_input"),
        output_data_units=_du("workflow_result"),
        triggers=_trig("envelope_input"),
    )

    b.add_link("workflow_input", "fetch.fetch_in", link_type="direct")
    b.add_link("fetch.protein_fasta", "align.align_in", link_type="direct")
    b.add_link("align.alignment", "conserve.conserve_in", link_type="direct")
    b.add_link("conserve.conservation_result", "report.report_in", link_type="direct")
    b.add_link("report.report", "envelope.envelope_input", link_type="direct")
    b.add_link("envelope.workflow_result", "workflow_output", link_type="direct")

    return b.load()


__all__ = ["build_viral_conserved_sites_workflow"]
