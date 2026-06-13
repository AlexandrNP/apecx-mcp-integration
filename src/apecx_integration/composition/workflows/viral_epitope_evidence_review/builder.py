"""viral_epitope_evidence_review — lightweight builder (Track D).

Catalog entry-point: a no-arg callable that constructs the workflow with
``nanobrain.lightweight.WorkflowBuilder`` and returns ``builder.load()``.

Pipeline (DAG with TWO fan-ins; each DirectLink inherits ``auto_transfer: true``
from config_version 2; each step reads the one bundle it needs out of the prior
step's output — no TransformLink, no ConditionalLink, no cycle):

    workflow_input {query, taxon_id?, protein?, requested_outputs?, design_approval_id?}
      → normalize   EvidenceQueryNormalizeStep     → params (passthrough; the DEPOSIT POINT)
          ├→ assemble  SynthesisContextAssemblyStep → bundle{query, rag, bvbrc, violin, pubs, globus}
          │    → structural StructuralEvidenceStep   → bundle + PDB/EMDB (merged) + structural_note
          │                                          ↘ merge.structural_in ┐
          ├→ sequence  SequenceConservationSubworkflowStep (nests viral_conserved_sites:    │ (FAN-IN #1)
          │            BV-BRC fetch → MAFFT MSA → conservation) → WorkflowResult|degrade ↘   │
          │                                                            merge.sequence_in ┘   │
          │    → merge  SequenceEvidenceMergeStep (FAN-IN #1) → bundle + conserved_sites/regions
          │            + a sequence_conservation stage report  → review.review_input         │
          │    → review EvidenceReviewSynthesisStep → {markdown} (LLM + deterministic structural)
          │                                            ↘ gate.review_in
          └────────────────────────────────────────────→ gate.control_in   (FAN-IN #2 edge)
      → gate        DesignGateStep (FAN-IN #2)     → {markdown, control_transfer?}
      → envelope    EnvelopeStep                   → WorkflowResult (ok | needs_input)
      → workflow_output

    SEQUENCE-CONSERVATION STAGE (E2-C1): the ``sequence`` step is a concrete
    ``SubworkflowStep`` that nests the existing ``viral_conserved_sites`` workflow via the
    ``inner_workflow_builder`` seam (its no-arg ``build_viral_conserved_sites_workflow``). It
    runs from the SAME query/taxon/protein the normalize step fans out, then ``merge`` folds the
    structured conservation result (``conserved_sites`` / ``conserved_regions``, recovered from
    the conserved-sites terminal handle) into the evidence bundle BEFORE synthesis, and emits a
    ``sequence_conservation`` stage report into the ``### Reasoning trace``. Conserved positions
    ride along in the bundle for the later structural stage (map onto 3D structure). DEGRADE-LOUD:
    the sequence step NEVER raises (it would strand the merge fan-in and silently empty the whole
    run — G127); on a sub-workflow failure or a query without a usable taxon_id/protein it returns
    a named marker that ``merge`` renders as a LOUD "sequence conservation unavailable: <reason>"
    note while the rest of the evidence still completes. Merge ALWAYS fires because BOTH its inputs
    (structural + sequence) always produce an output. Both fan-ins use ``AllDataReceivedTrigger``
    (the value-comparison re-arm is live).

    WHY normalize exists (load-bearing): run_workflow deposits the input under the
    catalog ``input_envelope_key`` = ``normalize.normalize_input`` — NOT the
    ``workflow_input`` DU. So control fields CANNOT be fanned from workflow_input
    (it never gets set on that path); they are captured at ``normalize`` and fanned
    out from its real output DU to BOTH assemble (query) and gate.control_in
    (requested_outputs / design_approval_id). The gate joins review_in + control_in
    via an AllDataReceivedTrigger (a fan-in, not a cycle — avoids the G99 cycle-test
    mandate). Verified by a real run_workflow integration test.

DU-NAME CONTRACT (load-bearing — do not rename): ``SynthesisContextAssemblyStep``
hard-codes its trigger-envelope unwrap to the key ``assembly_input``, so its input
DU MUST be ``assembly_input``. ``normalize`` is the entry/deposit step
(``normalize_input``). The custom steps unwrap their own declared keys
(``structural_input`` / ``review_input`` / ``normalize_input``).

Design output: ``requested_outputs="evidence_plus_design"`` opens the
``DesignGateStep``. Without a ``design_approval_id`` the gate returns
``needs_input`` (needs_prerequisite) — approval is EXPLICIT DATA, never inferred
from text — while still returning the gathered evidence. With an approval token it
appends a labelled design-hypotheses section (Phase B wires the evidence-bound LLM
generation; Phase A confirms the gate + provenance). Missing-``query`` gating is
automatic via RoC-2c (the entry step's ``step_input_schema`` below drives
``find_param_gaps`` at the run_workflow seam).
"""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.steps"


# RoC-2a — authoritative input contract on the ENTRY step (G6 step_input_schema).
# The entry step is ``normalize`` (its input DU ``normalize_input`` is the deposit
# point = the catalog ``input_envelope_key``). ``find_param_gaps`` unwraps this level
# to derive required params; ``obtain_via`` is an annotation for the frontier LLM.
EVIDENCE_INPUT_SCHEMA: dict[str, Any] = {
    "json_schema": {
        "type": "object",
        "properties": {
            "normalize_input": {
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
                        "obtain_via": (
                            "'evidence_only' (default) or 'evidence_plus_design' — the "
                            "latter needs a design_approval_id (the gate returns "
                            "needs_input if absent)"
                        ),
                    },
                    "design_approval_id": {
                        "type": "string",
                        "obtain_via": (
                            "approval token from the approval control plane (approve); "
                            "required only when requested_outputs='evidence_plus_design'"
                        ),
                    },
                },
                "required": ["query"],
            }
        },
        "required": ["normalize_input"],
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

    # Entry step (the deposit point = catalog input_envelope_key=normalize_input).
    # It captures the params and fans them out to BOTH assemble (query) and the gate
    # (control fields) — because run_workflow deposits under THIS step's input DU, NOT
    # the workflow_input DU, so control state CANNOT be fanned from workflow_input.
    # step_input_schema here is the RoC-2c required-input source.
    b.add_step(
        "normalize",
        f"{_STEPS}.evidence_query_normalize_step.EvidenceQueryNormalizeStep",
        step_input_schema=EVIDENCE_INPUT_SCHEMA,
        input_data_units=_du("normalize_input"),
        output_data_units=_du("normalize_out"),
        triggers=_trig("normalize_input"),
    )
    b.add_step(
        "assemble",
        f"{_STEPS}.synthesis_context_assembly_step.SynthesisContextAssemblyStep",
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
    # SEQUENCE-CONSERVATION leg (E2-C1): a concrete SubworkflowStep nesting viral_conserved_sites
    # via the inner_workflow_builder seam. G117: this step's OWN input DU (sequence_params) MUST
    # differ from the inner workflow's first-step input DU (fetch_in). It NEVER raises (degrade-
    # loud subclass) so the merge fan-in below always fires. settle_ms/timeout cover the real
    # BV-BRC fetch + MAFFT alignment of the inner cascade.
    b.add_step(
        "sequence",
        f"{_STEPS}.sequence_conservation_subworkflow_step.SequenceConservationSubworkflowStep",
        # Generous inner-cascade budget: the nested fetch→MAFFT→conserve runs CONCURRENTLY with
        # the assemble/structural network branches, so MAFFT (25 long polyprotein sequences) gets
        # less wall-clock than in isolation. Stays well under the catalog's 600s outer timeout.
        timeout_seconds=480.0,
        settle_ms=500,
        input_data_units=_du("sequence_params"),
        output_data_units=_du("sequence_result"),
        triggers=_trig("sequence_params"),
    )
    # FAN-IN #1: join the structural bundle (structural_in) with the sequence-conservation result
    # (sequence_in) via an AllDataReceivedTrigger, fold conserved_sites/regions into the bundle,
    # and emit the sequence_conservation stage report. Both inputs always arrive (each leg
    # degrades loud rather than failing), so this fan-in always fires.
    b.add_step(
        "merge",
        f"{_STEPS}.sequence_evidence_merge_step.SequenceEvidenceMergeStep",
        input_data_units={**_du("structural_in"), **_du("sequence_in")},
        output_data_units=_du("merged_bundle"),
        triggers=[
            {
                "class": "nanobrain.core.trigger.AllDataReceivedTrigger",
                "trigger_type": "all_data_received",
                "data_units": ["structural_in", "sequence_in"],
            }
        ],
    )
    b.add_step(
        "review",
        f"{_STEPS}.evidence_review_synthesis_step.EvidenceReviewSynthesisStep",
        input_data_units=_du("review_input"),
        output_data_units=_du("review_output"),
        triggers=_trig("review_input"),
    )
    # FAN-IN gate: joins the synthesized evidence (review_in) with the ORIGINAL
    # control fields (control_in, fanned out from `normalize.normalize_out` — the
    # reused assembly/synthesis steps drop them) via an AllDataReceivedTrigger. It
    # emits {markdown, control_transfer?} for the terminal EnvelopeStep. This is the
    # nanobrain-native control-state threading (fan-in, not a cycle).
    b.add_step(
        "gate",
        f"{_STEPS}.design_gate_step.DesignGateStep",
        input_data_units={**_du("review_in"), **_du("control_in")},
        output_data_units=_du("gate_output"),
        triggers=[
            {
                "class": "nanobrain.core.trigger.AllDataReceivedTrigger",
                "trigger_type": "all_data_received",
                "data_units": ["review_in", "control_in"],
            }
        ],
    )
    # Terminal EnvelopeStep shapes the gate's {markdown, control_transfer?} into the
    # standard WorkflowResult (the form run_workflow recognizes). control_transfer
    # present → needs_input; absent → ok.
    b.add_step(
        "envelope",
        f"{_STEPS}.envelope_step.EnvelopeStep",
        input_data_units=_du("envelope_input"),
        output_data_units=_du("workflow_result"),
        triggers=_trig("envelope_input"),
    )

    b.add_link("workflow_input", "normalize.normalize_input", link_type="direct")
    b.add_link("normalize.normalize_out", "assemble.assembly_input", link_type="direct")
    # Fan-out from a REAL output DU (normalize_out, which the deposit actually sets):
    # the control fields reach the gate directly, bypassing the steps that drop them.
    # This is the second edge of the fan-in into `gate`.
    b.add_link("normalize.normalize_out", "gate.control_in", link_type="direct")
    # Third fan-out edge from normalize_out: feed the query/taxon/protein to the nested
    # conserved-sites subworkflow (the sequence step reads taxon_id + protein).
    b.add_link("normalize.normalize_out", "sequence.sequence_params", link_type="direct")
    b.add_link(
        "assemble.synthesis_bundle_output", "structural.structural_input", link_type="direct"
    )
    # FAN-IN #1 edges into `merge`: the structural bundle + the sequence-conservation result.
    b.add_link("structural.structural_bundle", "merge.structural_in", link_type="direct")
    b.add_link("sequence.sequence_result", "merge.sequence_in", link_type="direct")
    # The enriched bundle (now carrying conserved_sites/regions + the sequence stage report)
    # feeds the synthesis step.
    b.add_link("merge.merged_bundle", "review.review_input", link_type="direct")
    b.add_link("review.review_output", "gate.review_in", link_type="direct")
    b.add_link("gate.gate_output", "envelope.envelope_input", link_type="direct")
    b.add_link("envelope.workflow_result", "workflow_output", link_type="direct")

    return b


def build_viral_epitope_evidence_review_workflow():
    """Construct + load the viral_epitope_evidence_review workflow (catalog entry-point)."""
    return _evidence_workflow_builder().load()


__all__ = ["EVIDENCE_INPUT_SCHEMA", "build_viral_epitope_evidence_review_workflow"]
