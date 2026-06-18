"""conserved_epitope_candidate_assessment - approved follow-up candidate assessment."""

from apecx_integration.composition.workflows.conserved_epitope_candidate_assessment.builder import (
    CANDIDATE_ASSESSMENT_INPUT_SCHEMA,
    build_conserved_epitope_candidate_assessment_workflow,
)

__all__ = [
    "CANDIDATE_ASSESSMENT_INPUT_SCHEMA",
    "build_conserved_epitope_candidate_assessment_workflow",
]
