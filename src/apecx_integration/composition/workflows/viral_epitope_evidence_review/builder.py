"""viral_epitope_evidence_review — lightweight builder (Track D).

Catalog entry-point: a no-arg callable that constructs the workflow with
``nanobrain.lightweight.WorkflowBuilder`` and returns ``builder.load()``.

Pipeline (linear DAG; each DirectLink inherits ``auto_transfer: true`` from
config_version 2; each step reads the one bundle it needs out of the prior
step's output — no TransformLink, no ConditionalLink, no cycle):

    workflow_input {query, taxon_id?, protein?, requested_outputs?, ...}
      → assemble    SynthesisContextAssemblyStep   → bundle{query, rag, bvbrc, violin, pubs, globus}
      → structural  StructuralEvidenceStep         → bundle + PDB/EMDB (merged into globus_results)
                                                       + structural_records + structural_note (loud no-hit)
      → review      EvidenceReviewSynthesisStep    → {markdown}  (LLM synthesis + deterministic
                                                       structural section)
      → envelope    EnvelopeStep                   → WorkflowResult
      → workflow_output

DU-NAME CONTRACT (load-bearing — do not rename): ``SynthesisContextAssemblyStep``
hard-codes its trigger-envelope unwrap to the key ``assembly_input`` (and emits
its bundle as the step's return), so the assemble step's input DU MUST be
``assembly_input``. The other two custom steps unwrap their own declared input
key (``structural_input`` / ``review_input``). EnvelopeStep unwraps generically.

Design output (``requested_outputs="evidence_plus_design"`` + approval gating) is
a deliberate v1.1 follow-up: it needs control-state (``requested_outputs`` /
``design_approval_id``) to reach a terminal gate, which the reused closed steps
drop — the nanobrain-native fix is an ``AllDataReceivedTrigger`` fan-in, which is
being verified separately before it is built on. v1 ships the evidence core
(``requested_outputs`` is accepted in the schema but only ``evidence_only`` is
honored). Missing-``query`` gating is automatic via RoC-2c (the entry step's
``step_input_schema`` below drives ``find_param_gaps`` at the run_workflow seam).
"""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.steps"


# RoC-2a — authoritative input contract on the ENTRY step (G6 step_input_schema).
# Wrapped shape: the entry data unit ``assembly_input`` holds the parameter dict.
# ``find_param_gaps`` unwraps this level to derive required params; ``obtain_via``
# is an annotation for the frontier LLM (jsonschema ignores unknown keywords).
EVIDENCE_INPUT_SCHEMA: dict[str, Any] = {
    "json_schema": {
        "type": "object",
        "properties": {
            "assembly_input": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "obtain_via": "the scientist's evidence question (free text)",
                    },
                    "taxon_id": {
                        "type": "integer",
                        "obtain_via": "optional NCBI taxon id to focus the query",
                    },
                    "protein": {
                        "type": "string",
                        "obtain_via": "optional protein/antigen name to focus structural lookup",
                    },
                    "requested_outputs": {
                        "type": "string",
                        "obtain_via": "'evidence_only' (default) — 'evidence_plus_design' is v1.1",
                    },
                },
                "required": ["query"],
            }
        },
        "required": ["assembly_input"],
    }
}


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def _trig(du: str) -> list[dict[str, Any]]:
    return [{"class": _TRIGGER, "data_unit": du}]


def _evidence_workflow_builder():
    """Build (but do NOT load) the WorkflowBuilder. Exposed so tests can exercise
    the get_config()→YAML→from_config path (multi-path construction parity) without
    re-deriving the topology. ``build_..._workflow`` is just this + ``.load()``."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "viral_epitope_evidence_review",
        "Assemble + synthesize evidence (RAG, VIOLIN/BV-BRC, PubMed, PDB/EMDB "
        "structures) for a viral epitope/antigen/protein question.",
    )
    b.add_input("workflow_input", "DataUnitMemory")
    b.add_output("workflow_output", "DataUnitMemory")

    # Entry step: its input DU MUST be ``assembly_input`` (the step's hard-coded
    # unwrap key). step_input_schema makes it the RoC-2c required-input source.
    b.add_step(
        "assemble",
        f"{_STEPS}.synthesis_context_assembly_step.SynthesisContextAssemblyStep",
        step_input_schema=EVIDENCE_INPUT_SCHEMA,
        input_data_units=_du("assembly_input"),
        output_data_units=_du("synthesis_bundle_output"),
        triggers=_trig("assembly_input"),
    )
    b.add_step(
        "structural",
        f"{_STEPS}.structural_evidence_step.StructuralEvidenceStep",
        input_data_units=_du("structural_input"),
        output_data_units=_du("structural_bundle"),
        triggers=_trig("structural_input"),
    )
    b.add_step(
        "review",
        f"{_STEPS}.evidence_review_synthesis_step.EvidenceReviewSynthesisStep",
        input_data_units=_du("review_input"),
        output_data_units=_du("review_output"),
        triggers=_trig("review_input"),
    )
    b.add_step(
        "envelope",
        f"{_STEPS}.envelope_step.EnvelopeStep",
        input_data_units=_du("envelope_input"),
        output_data_units=_du("workflow_result"),
        triggers=_trig("envelope_input"),
    )

    b.add_link("workflow_input", "assemble.assembly_input", link_type="direct")
    b.add_link(
        "assemble.synthesis_bundle_output", "structural.structural_input", link_type="direct"
    )
    b.add_link("structural.structural_bundle", "review.review_input", link_type="direct")
    b.add_link("review.review_output", "envelope.envelope_input", link_type="direct")
    b.add_link("envelope.workflow_result", "workflow_output", link_type="direct")

    return b


def build_viral_epitope_evidence_review_workflow():
    """Construct + load the viral_epitope_evidence_review workflow (catalog entry-point)."""
    return _evidence_workflow_builder().load()


__all__ = ["EVIDENCE_INPUT_SCHEMA", "build_viral_epitope_evidence_review_workflow"]
