"""MCP tool for EEEV epitope analysis workflow.

This tool provides specialized end-to-end epitope analysis for Eastern Equine
Encephalitis Virus (EEEV) using the validated EEEV-specific workflow. It combines
multi-source retrieval (VIOLIN, BV-BRC, Globus, PubMed, RAG) with targeted
synthesis for epitope identification.

How this differs from generic ``synthesize_query``
--------------------------------------------------
- Uses EEEV-specific query structuring and entity extraction
- Optimized for epitope/vaccine-related information retrieval
- Returns structured epitope analysis with conservation and accessibility data
- Integrates viral strain comparison from BV-BRC genomes
- Provides VIOLIN vaccine ontology mappings for discovered epitopes

The tool automatically detects EEEV-related queries and provides targeted analysis.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_EEEV_ASSEMBLY_STEP: Any = None
_EEEV_SYNTHESIS_STEP: Any = None
_EEEV_LOAD_ERROR: str | None = None


def _is_eeev_query(query: str) -> bool:
    """Detect if a query is related to EEEV epitope analysis.

    Returns True if the query mentions EEEV, Eastern Equine Encephalitis,
    epitopes, envelope proteins, or related vaccine/immunology terms.
    """
    query_lower = query.lower()

    eeev_terms = [
        "eeev",
        "eastern equine encephalitis",
        "eastern equine encephalitis virus",
        "alphavirus",
        "togaviridae",
    ]

    epitope_terms = [
        "epitope",
        "epitopes",
        "neutralizing",
        "antibody binding",
        "vaccine",
        "immunogenic",
        "antigenic",
        "envelope protein",
        "glycoprotein",
        "conserved",
        "accessible",
    ]

    has_eeev = any(term in query_lower for term in eeev_terms)
    has_epitope = any(term in query_lower for term in epitope_terms)

    return has_eeev and has_epitope


def _load_eeev_steps() -> tuple[Any, Any] | tuple[None, None]:
    """Lazy-load and cache the EEEV assembly + synthesis step instances.

    Uses the steps from our working EEEV workflow configuration.
    """
    global _EEEV_ASSEMBLY_STEP, _EEEV_SYNTHESIS_STEP, _EEEV_LOAD_ERROR
    if _EEEV_ASSEMBLY_STEP is not None and _EEEV_SYNTHESIS_STEP is not None:
        return _EEEV_ASSEMBLY_STEP, _EEEV_SYNTHESIS_STEP
    if _EEEV_LOAD_ERROR is not None:
        return None, None

    try:
        from apecx_integration.composition.steps.rag_synthesis_step import RagSynthesisStep
        from apecx_integration.composition.steps.synthesis_context_assembly_step import (
            SynthesisContextAssemblyStep,
        )

        # Use the step configs from our apecx-mcp-integration directory
        here = Path(__file__).resolve()
        # tools/eeev_epitope_analysis.py → apecx-mcp-integration root is parents[4]
        repo_root = here.parents[4]

        assembly_yaml = repo_root / "steps" / "synthesis_context_assembly.yml"
        synthesis_yaml = repo_root / "steps" / "rag_synthesis.yml"

        if not assembly_yaml.is_file():
            _EEEV_LOAD_ERROR = (
                f"EEEV assembly step YAML not found at {assembly_yaml}. "
                "The EEEV workflow steps must be present for EEEV epitope analysis."
            )
            return None, None
        if not synthesis_yaml.is_file():
            _EEEV_LOAD_ERROR = f"EEEV synthesis step YAML not found at {synthesis_yaml}."
            return None, None

        _EEEV_ASSEMBLY_STEP = SynthesisContextAssemblyStep.from_config(assembly_yaml)
        _EEEV_SYNTHESIS_STEP = RagSynthesisStep.from_config(synthesis_yaml)
        log.info("EEEV epitope analysis: loaded assembly + synthesis steps")
    except Exception as exc:
        _EEEV_LOAD_ERROR = f"Failed to load EEEV pipeline: {type(exc).__name__}: {exc}"
        log.warning(_EEEV_LOAD_ERROR)
        return None, None

    return _EEEV_ASSEMBLY_STEP, _EEEV_SYNTHESIS_STEP


def _structure_eeev_query(query: str) -> dict[str, Any]:
    """Structure a query for EEEV epitope analysis.

    Extracts relevant terms and prepares the input format expected by
    the SynthesisContextAssemblyStep.
    """
    # Extract key terms related to EEEV and epitopes
    query_terms = []

    # EEEV-specific terms
    if re.search(r"\beeev\b", query, re.IGNORECASE):
        query_terms.append("EEEV")
    if re.search(r"eastern equine encephalitis", query, re.IGNORECASE):
        query_terms.append("EEEV")

    # Epitope/immunology terms
    epitope_keywords = [
        "neutralizing",
        "epitope",
        "epitopes",
        "envelope",
        "glycoprotein",
        "conserved",
        "antibody",
        "binding",
        "accessible",
        "immunogenic",
        "antigenic",
        "vaccine",
    ]

    for keyword in epitope_keywords:
        if re.search(rf"\b{re.escape(keyword)}\b", query, re.IGNORECASE):
            query_terms.append(keyword)

    # Remove duplicates while preserving order
    query_terms = list(dict.fromkeys(query_terms))

    return {
        "query": query.strip(),
        "entities": None,  # Let the system extract entities automatically
        "query_terms": query_terms,
    }


async def analyze_eeev_epitopes(
    query: str,
    skip_pubmed: bool = False,
) -> dict:
    """Perform comprehensive EEEV epitope analysis.

    Executes the specialized EEEV epitope analysis workflow that integrates:
    - VIOLIN database pathogen and vaccine information
    - BV-BRC viral genomes for strain comparison
    - Globus Search for structural data (PDB, CryoEM)
    - PubMed literature (when available)
    - Domain RAG semantic search

    Args:
        query: The EEEV epitope research question. Should mention EEEV/Eastern
            Equine Encephalitis and epitope-related terms for best results.
        skip_pubmed: When True, skip PubMed literature search (offline mode).

    Returns:
        On success: {
            "analysis": "<comprehensive epitope analysis>",
            "epitopes_found": <number>,
            "data_sources": {
                "rag_chunks": <int>, "violin_mappings": <int>,
                "bvbrc_genomes": <int>, "publications": <int>,
                "globus_structures": <int>
            },
            "query_type": "eeev_epitope_analysis"
        }
        On error: {"error": "<message>"}
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string"}

    # Check if this is an appropriate EEEV query
    if not _is_eeev_query(query):
        return {
            "error": "Query does not appear to be related to EEEV epitope analysis. "
            "Please include terms like 'EEEV', 'Eastern Equine Encephalitis', "
            "'epitopes', 'envelope protein', or 'neutralizing antibodies'."
        }

    assembly, synthesis = _load_eeev_steps()
    if assembly is None or synthesis is None:
        return {"error": _EEEV_LOAD_ERROR or "EEEV analysis pipeline not loaded"}

    # Structure the query for EEEV analysis
    structured_query = _structure_eeev_query(query)
    log.info(f"EEEV analysis for query: {structured_query['query'][:100]}...")
    log.info(f"Extracted terms: {structured_query['query_terms']}")

    # Override skip_pubmed if requested
    prior_skip = getattr(assembly, "_skip_pubmed", False)
    assembly._skip_pubmed = bool(skip_pubmed)

    try:
        # Execute assembly step with structured EEEV query
        bundle = await assembly.process({"assembly_input": structured_query})

        # Execute synthesis step
        result = await synthesis.process({"synthesis_input": bundle})

        # Count potential epitopes mentioned in the analysis
        analysis_text = result.get("synthesis", "")
        epitope_count = len(
            re.findall(r"\bepitope|\bpeptide|\bVO_\d+", analysis_text, re.IGNORECASE)
        )

    except ValueError as exc:
        return {"error": f"EEEV analysis failed: {exc}"}
    except Exception as exc:
        return {"error": f"EEEV analysis error: {type(exc).__name__}: {exc}"}
    finally:
        assembly._skip_pubmed = prior_skip

    return {
        "analysis": result.get("synthesis", ""),
        "epitopes_found": epitope_count,
        "data_sources": {
            "rag_chunks": len(bundle.get("rag_chunks", [])),
            "violin_mappings": len(bundle.get("violin_mappings", [])),
            "bvbrc_genomes": len(bundle.get("bvbrc_genomes", [])),
            "publications": len(bundle.get("publications", [])),
            "globus_structures": len(bundle.get("globus_results", [])),
        },
        "query_type": "eeev_epitope_analysis",
    }


__all__ = ["analyze_eeev_epitopes", "_is_eeev_query"]
