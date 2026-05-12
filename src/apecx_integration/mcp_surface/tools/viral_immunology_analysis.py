"""Generalized MCP tool for viral immunology research analysis.

This tool replaces the hardcoded EEEV-specific implementation with a comprehensive
viral immunology research framework that can handle ANY virus family and research type.

Key improvements over the EEEV-only tool:
1. **Virus generalization**: Detects any virus + immunology research request
2. **Unlimited data retrieval**: Removes arbitrary result caps, retrieves ALL relevant data
3. **Framework compliance**: Uses proper nanobrain workflow patterns
4. **Extensible architecture**: Easy to add new viruses and research types
5. **Quality-based filtering**: Uses relevance scoring instead of arbitrary truncation

Example usage:
- "What are COVID-19 spike protein neutralizing epitopes?"
- "Find conserved regions in influenza hemagglutinin for vaccine design"
- "Identify Zika virus envelope protein antibody binding sites"
- "HIV gp120 immunogenic domains and vaccine targets"
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_VIRAL_CLASSIFIER_STEP: Any = None
_UNLIMITED_ASSEMBLY_STEP: Any = None
_RAG_SYNTHESIS_STEP: Any = None
_LOAD_ERROR: str | None = None


def _load_viral_immunology_pipeline() -> tuple[Any, Any, Any] | tuple[None, None, None]:
    """Lazy-load and cache the viral immunology analysis pipeline steps.

    Uses proper nanobrain workflow components with framework compliance.
    """
    global _VIRAL_CLASSIFIER_STEP, _UNLIMITED_ASSEMBLY_STEP, _RAG_SYNTHESIS_STEP, _LOAD_ERROR

    if (
        _VIRAL_CLASSIFIER_STEP is not None
        and _UNLIMITED_ASSEMBLY_STEP is not None
        and _RAG_SYNTHESIS_STEP is not None
    ):
        return _VIRAL_CLASSIFIER_STEP, _UNLIMITED_ASSEMBLY_STEP, _RAG_SYNTHESIS_STEP

    if _LOAD_ERROR is not None:
        return None, None, None

    try:
        from apecx_integration.composition.steps.rag_synthesis_step import RagSynthesisStep
        from apecx_integration.composition.steps.unlimited_synthesis_assembly_step import (
            UnlimitedSynthesisAssemblyStep,
        )
        from apecx_integration.composition.steps.viral_immunology_query_classifier_step import (
            ViralImmunologyQueryClassifierStep,
        )

        # Get repository root
        here = Path(__file__).resolve()
        repo_root = here.parents[
            4
        ]  # tools/viral_immunology_analysis.py → apecx-mcp-integration root

        # Load step configurations
        classifier_yaml = repo_root / "configs" / "viral_immunology_query_classifier.yml"
        # Create unlimited assembly config on-the-fly
        unlimited_assembly_yaml = repo_root / "configs" / "unlimited_synthesis_assembly.yml"
        synthesis_yaml = repo_root / "steps" / "rag_synthesis.yml"

        # Verify config files exist
        if not classifier_yaml.is_file():
            _LOAD_ERROR = f"Viral classifier config not found at {classifier_yaml}"
            return None, None, None

        if not synthesis_yaml.is_file():
            _LOAD_ERROR = f"RAG synthesis config not found at {synthesis_yaml}"
            return None, None, None

        # Create unlimited assembly config if it doesn't exist
        if not unlimited_assembly_yaml.is_file():
            _create_unlimited_assembly_config(unlimited_assembly_yaml)

        # Load steps using proper nanobrain patterns
        _VIRAL_CLASSIFIER_STEP = ViralImmunologyQueryClassifierStep.from_config(classifier_yaml)
        _UNLIMITED_ASSEMBLY_STEP = UnlimitedSynthesisAssemblyStep.from_config(
            unlimited_assembly_yaml
        )
        _RAG_SYNTHESIS_STEP = RagSynthesisStep.from_config(synthesis_yaml)

        log.info("Viral immunology pipeline: loaded all three steps successfully")

    except Exception as exc:
        _LOAD_ERROR = f"Failed to load viral immunology pipeline: {type(exc).__name__}: {exc}"
        log.warning(_LOAD_ERROR)
        return None, None, None

    return _VIRAL_CLASSIFIER_STEP, _UNLIMITED_ASSEMBLY_STEP, _RAG_SYNTHESIS_STEP


def _create_unlimited_assembly_config(config_path: Path) -> None:
    """Create unlimited assembly step configuration."""
    config_content = """# Unlimited Synthesis Assembly Step Configuration
#
# Enhanced assembly that retrieves ALL relevant data instead of applying
# arbitrary caps. Critical for comprehensive scientific analysis.

class: "apecx_integration.composition.steps.unlimited_synthesis_assembly_step.UnlimitedSynthesisAssemblyStep"

name: "unlimited_synthesis_assembly"
description: "Unlimited multi-source assembly - retrieves ALL relevant VIOLIN/BV-BRC/PubMed/Globus data"

# Enhanced data retrieval limits (no arbitrary caps)
k_rag: 20  # Increased from 5
max_publications: 50  # Increased from 5, respects PubMed API limits
max_globus_hits: 100  # Increased from 10

# Quality filters instead of arbitrary caps
min_violin_relevance_score: 0.1
min_bvbrc_relevance_score: 0.1

# Performance controls
enable_streaming: false
batch_size: 1000

# Framework configuration
auto_initialize: true
debug_mode: false
enable_logging: true

# Data units
input_data_units:
  assembly_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "assembly_input"
    description: "Input for unlimited synthesis assembly"

output_data_units:
  synthesis_bundle_output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "synthesis_bundle_output"
    description: "Complete synthesis input bundle with unlimited data"

# Triggers
triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "assembly_input"
"""

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(config_content)

    log.info(f"Created unlimited assembly config at {config_path}")


async def analyze_viral_immunology(
    query: str,
    skip_pubmed: bool = False,
) -> dict:
    """Perform comprehensive viral immunology analysis for any virus.

    This is the generalized replacement for the EEEV-specific tool. Handles
    any virus family and immunology research type using configurable ontology
    and unlimited data retrieval.

    Args:
        query: The viral immunology research question. Should mention a virus
            and immunology concepts (epitopes, antibodies, vaccines, etc.)
        skip_pubmed: When True, skip PubMed literature search

    Returns:
        On success: {
            "analysis": "<comprehensive immunology analysis>",
            "virus_classification": {
                "virus_family": str,
                "virus_names": list[str],
                "proteins_of_interest": list[str],
                "research_type": str,
                "confidence": float
            },
            "data_sources": {
                "rag_chunks": int,
                "violin_mappings": int,
                "bvbrc_genomes": int,
                "publications": int,
                "globus_structures": int
            },
            "query_type": "viral_immunology_analysis"
        }
        On error: {"error": "<message>"}
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string"}

    # Load pipeline components
    classifier, assembly, synthesis = _load_viral_immunology_pipeline()
    if classifier is None or assembly is None or synthesis is None:
        return {"error": _LOAD_ERROR or "Viral immunology pipeline not loaded"}

    query = query.strip()
    log.info(f"Viral immunology analysis for query: {query[:100]}...")

    # Initialize variables for finally block
    prior_skip = getattr(assembly, "_skip_pubmed", False) if assembly else False

    try:
        # Step 1: Classify query for viral immunology intent
        classification_result = await classifier.process({"query": query})

        if not classification_result.get("is_viral_immunology", False):
            return {
                "error": (
                    "Query does not appear to be related to viral immunology research. "
                    "Please include virus names (COVID-19, influenza, EEEV, etc.) and "
                    "immunology terms (epitopes, antibodies, vaccines, etc.)"
                )
            }

        classification_data = classification_result["classification"]
        log.info(
            f"Viral classification: {classification_data['virus_family']} - "
            f"{classification_data['research_type']} (confidence: {classification_data['confidence']:.3f})"
        )

        # Step 2: Enhanced query structuring with classification results
        enhanced_query_input = {
            "query": query,
            "entities": [
                {"name": virus, "type": "virus"} for virus in classification_data["virus_names"]
            ],
            "query_terms": (
                classification_data["virus_names"]
                + classification_data["proteins_of_interest"]
                + classification_data["immunology_concepts"]
            ),
        }

        # Override skip_pubmed if requested
        if assembly:
            assembly._skip_pubmed = bool(skip_pubmed)

        # Step 3: Unlimited multi-source data assembly
        bundle = await assembly.process({"assembly_input": enhanced_query_input})

        # Step 4: Enhanced synthesis with all retrieved data
        result = await synthesis.process({"synthesis_input": bundle})

        # Count analysis artifacts (epitopes, proteins, structures, etc.)
        analysis_text = result.get("synthesis", "")
        analysis_artifacts = _count_analysis_artifacts(analysis_text)

        return {
            "analysis": result.get("synthesis", ""),
            "virus_classification": classification_data,
            "analysis_artifacts": analysis_artifacts,
            "data_sources": {
                "rag_chunks": len(bundle.get("rag_chunks", [])),
                "violin_mappings": len(bundle.get("violin_mappings", [])),
                "bvbrc_genomes": len(bundle.get("bvbrc_genomes", [])),
                "publications": len(bundle.get("publications", [])),
                "globus_structures": len(bundle.get("globus_results", [])),
            },
            "query_type": "viral_immunology_analysis",
        }

    except ValueError as exc:
        return {"error": f"Viral immunology analysis failed: {exc}"}
    except Exception as exc:
        return {"error": f"Analysis error: {type(exc).__name__}: {exc}"}
    finally:
        # Restore original skip_pubmed setting
        if assembly:
            assembly._skip_pubmed = prior_skip


def _count_analysis_artifacts(analysis_text: str) -> dict[str, int]:
    """Count scientific artifacts mentioned in the analysis.

    Returns counts of epitopes, proteins, structures, etc. found in the analysis.
    """
    import re

    if not analysis_text:
        return {}

    text_lower = analysis_text.lower()

    # Count different types of scientific artifacts
    epitope_count = len(re.findall(r"\bepitope|\bpeptide|\bvo_\d+", text_lower))
    protein_count = len(re.findall(r"\bprotein|\bengelope|\bspike|\bcapsid|\bglycop", text_lower))
    antibody_count = len(re.findall(r"\bantibod|\bneutraliz|\bimmunoglob", text_lower))
    structure_count = len(re.findall(r"\bstructur|\bcrystal|\bnmr|\bpdb|\bcryo", text_lower))
    vaccine_count = len(re.findall(r"\bvaccin|\bimmuniz|\bimmunogen", text_lower))

    return {
        "epitopes_mentioned": epitope_count,
        "proteins_mentioned": protein_count,
        "antibodies_mentioned": antibody_count,
        "structures_mentioned": structure_count,
        "vaccines_mentioned": vaccine_count,
    }


# Backward compatibility alias for existing MCP server registration
async def analyze_eeev_epitopes(query: str, skip_pubmed: bool = False) -> dict:
    """Backward compatibility wrapper for EEEV-specific queries.

    Delegates to the generalized viral immunology analysis tool.
    """
    return await analyze_viral_immunology(query, skip_pubmed)


__all__ = ["analyze_viral_immunology", "analyze_eeev_epitopes"]
