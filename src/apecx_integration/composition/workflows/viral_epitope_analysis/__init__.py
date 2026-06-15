"""viral_epitope_analysis — identify and analyze B-cell epitopes / antigenic sites on a
viral protein. Pulls the data itself (BV-BRC genomes, PDB/EMDB structures, VIOLIN, PubMed)
and analyzes it: sequence-conservation (MAFFT) + structural surface-exposure (PyMOL SASA) →
ranked conserved, surface-exposed epitope candidates with cited evidence.

Built the lightweight way (``nanobrain.lightweight.WorkflowBuilder``)."""

from apecx_integration.composition.workflows.viral_epitope_analysis.builder import (
    EVIDENCE_INPUT_SCHEMA,
    build_viral_epitope_analysis_workflow,
)

__all__ = [
    "EVIDENCE_INPUT_SCHEMA",
    "build_viral_epitope_analysis_workflow",
]
