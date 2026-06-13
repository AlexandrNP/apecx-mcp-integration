"""viral_epitope_evidence_review — evidence bundle for a viral epitope/antigen/
protein question, assembled from RAG + VIOLIN/BV-BRC + PubMed + PDB/EMDB
structural references and synthesized into grounded Markdown.

Built the lightweight way (``nanobrain.lightweight.WorkflowBuilder``)."""

from apecx_integration.composition.workflows.viral_epitope_evidence_review.builder import (
    EVIDENCE_INPUT_SCHEMA,
    build_viral_epitope_evidence_review_workflow,
)

__all__ = [
    "EVIDENCE_INPUT_SCHEMA",
    "build_viral_epitope_evidence_review_workflow",
]
