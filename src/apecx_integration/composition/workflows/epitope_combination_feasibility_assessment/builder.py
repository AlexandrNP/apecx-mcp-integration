"""epitope_combination_feasibility_assessment - lightweight nanobrain builder."""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.steps"

COMBINATION_FEASIBILITY_INPUT_SCHEMA: dict[str, Any] = {
    "json_schema": {
        "type": "object",
        "properties": {
            "combination_request": {
                "type": "object",
                "properties": {
                    "evidence_data_handle": {
                        "type": "string",
                        "obtain_via": ("optional data_handle from the upstream evidence run"),
                    },
                    "evidence_bundle": {
                        "type": "object",
                        "obtain_via": "inline evidence Bundle payload; use only when no handle exists",
                    },
                    "candidate_assessment_handle": {
                        "type": "string",
                        "obtain_via": (
                            "data_handle returned by the approved candidate-peptide assessment"
                        ),
                    },
                    "candidate_assessment_bundle": {
                        "type": "object",
                        "obtain_via": (
                            "inline approved candidate-peptide Bundle; use only when no handle exists"
                        ),
                    },
                    "additional_epitopes": {
                        "type": "array",
                        "items": {"type": "object"},
                        "obtain_via": (
                            "one or more additional epitopes with source metadata, "
                            "coordinates, and optional sequence"
                        ),
                    },
                    "assessment_question": {
                        "type": "string",
                        "obtain_via": "optional follow-up assessment question used for approval scoping",
                    },
                    "design_approval_id": {
                        "type": "string",
                        "obtain_via": (
                            "approval token from the approval control plane; required before "
                            "epitope-level combination output is released"
                        ),
                    },
                },
                "required": [],
            }
        },
        "required": ["combination_request"],
    }
}


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def _trig(du: str) -> list[dict[str, Any]]:
    return [{"class": _TRIGGER, "data_unit": du}]


def _combination_feasibility_workflow_builder():
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "epitope_combination_feasibility_assessment",
        "Assess whether an approved candidate peptide and additional epitopes can be considered "
        "together as an evidence-limited epitope combination.",
    )
    b.add_input("workflow_input", "DataUnitMemory")
    b.add_output("workflow_output", "DataUnitMemory")

    # Single-responsibility pipeline: intake (load/validate) -> classify (deterministic
    # classification) -> release (approval gate + render) -> envelope (wrap). The entry step
    # owns the input schema and an input DU literally named ``combination_request`` so the
    # catalog ``input_envelope_key`` deposit lands here.
    b.add_step(
        "intake",
        f"{_STEPS}.combination_intake_step.CombinationIntakeStep",
        step_input_schema=COMBINATION_FEASIBILITY_INPUT_SCHEMA,
        input_data_units=_du("combination_request"),
        output_data_units=_du("intake_output"),
        triggers=_trig("combination_request"),
    )
    b.add_step(
        "classify",
        f"{_STEPS}.combination_classification_step.CombinationClassificationStep",
        input_data_units=_du("classify_input"),
        output_data_units=_du("classify_output"),
        triggers=_trig("classify_input"),
    )
    b.add_step(
        "release",
        f"{_STEPS}.combination_release_step.CombinationReleaseStep",
        input_data_units=_du("release_input"),
        output_data_units=_du("release_output"),
        triggers=_trig("release_input"),
    )
    b.add_step(
        "envelope",
        f"{_STEPS}.envelope_step.EnvelopeStep",
        input_data_units=_du("envelope_input"),
        output_data_units=_du("workflow_result"),
        triggers=_trig("envelope_input"),
    )

    # Required by static validation even though run_workflow deposits via the catalog
    # input_envelope_key (combination_request) — do NOT remove this edge.
    b.add_link("workflow_input", "intake.combination_request", link_type="direct")
    b.add_link("intake.intake_output", "classify.classify_input", link_type="direct")
    b.add_link("classify.classify_output", "release.release_input", link_type="direct")
    b.add_link("release.release_output", "envelope.envelope_input", link_type="direct")
    b.add_link("envelope.workflow_result", "workflow_output", link_type="direct")
    return b


def build_epitope_combination_feasibility_assessment_workflow():
    """Construct and load the epitope_combination_feasibility_assessment workflow."""
    return _combination_feasibility_workflow_builder().load()


__all__ = [
    "COMBINATION_FEASIBILITY_INPUT_SCHEMA",
    "build_epitope_combination_feasibility_assessment_workflow",
]
