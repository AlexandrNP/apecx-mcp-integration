"""conserved_epitope_candidate_assessment - lightweight nanobrain builder."""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.steps"

CANDIDATE_ASSESSMENT_INPUT_SCHEMA: dict[str, Any] = {
    "json_schema": {
        "type": "object",
        "properties": {
            "assessment_input": {
                "type": "object",
                "properties": {
                    "evidence_data_handle": {
                        "type": "string",
                        "obtain_via": (
                            "the data_handle returned by the upstream conserved-region "
                            "evidence workflow"
                        ),
                    },
                    "evidence_bundle": {
                        "type": "object",
                        "obtain_via": "inline Bundle payload; use only when no handle is available",
                    },
                    "assessment_question": {
                        "type": "string",
                        "obtain_via": "optional follow-up assessment question",
                    },
                    "design_approval_id": {
                        "type": "string",
                        "obtain_via": (
                            "approval token from the approval control plane; required before "
                            "candidate peptide sequence output is released"
                        ),
                    },
                },
                "required": [],
            }
        },
        "required": ["assessment_input"],
    }
}


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def _trig(du: str) -> list[dict[str, Any]]:
    return [{"class": _TRIGGER, "data_unit": du}]


def _candidate_assessment_workflow_builder():
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "conserved_epitope_candidate_assessment",
        "Assess an approved minimal consensus peptide candidate from prior conserved-region evidence.",
    )
    b.add_input("workflow_input", "DataUnitMemory")
    b.add_output("workflow_output", "DataUnitMemory")

    b.add_step(
        "assessment",
        f"{_STEPS}.peptide_candidate_assessment_step.PeptideCandidateAssessmentStep",
        step_input_schema=CANDIDATE_ASSESSMENT_INPUT_SCHEMA,
        input_data_units=_du("assessment_input"),
        output_data_units=_du("assessment_output"),
        triggers=_trig("assessment_input"),
    )
    b.add_step(
        "envelope",
        f"{_STEPS}.envelope_step.EnvelopeStep",
        input_data_units=_du("envelope_input"),
        output_data_units=_du("workflow_result"),
        triggers=_trig("envelope_input"),
    )

    b.add_link("workflow_input", "assessment.assessment_input", link_type="direct")
    b.add_link("assessment.assessment_output", "envelope.envelope_input", link_type="direct")
    b.add_link("envelope.workflow_result", "workflow_output", link_type="direct")
    return b


def build_conserved_epitope_candidate_assessment_workflow():
    """Construct and load the conserved_epitope_candidate_assessment workflow."""
    return _candidate_assessment_workflow_builder().load()


__all__ = [
    "CANDIDATE_ASSESSMENT_INPUT_SCHEMA",
    "build_conserved_epitope_candidate_assessment_workflow",
]
