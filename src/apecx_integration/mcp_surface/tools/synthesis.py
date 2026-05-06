"""MCP tool for the end-to-end RAG synthesis pipeline.

This tool exposes the rag_e2e_synthesis workflow (assembly + synthesis)
as a single one-shot MCP tool, bypassing the composer entirely. Use
when the operator wants a direct "ask the system a question, get a
grounded Markdown answer" path without going through workflow
composition.

How this differs from ``start_workflow``
----------------------------------------
``start_workflow`` (workflows.py) hands the description to the
Composer (T-COMP), which reads the natural-language prompt, retrieves
matching components from the catalog, and emits a fresh workflow YAML.
That round-trip is right when the request is novel or genuinely
multi-step; for the common synthesis question shape it is overhead
(one LLM call to compose + another to synthesize, both fallible).

``synthesize_query`` skips the Composer and drives the pre-built
``rag_e2e_synthesis_workflow`` two-step chain directly via the step
classes' ``from_config`` + ``process()`` methods. One LLM round-trip
(synthesis only). The retrieval branches run concurrently inside the
assembly step.

Both step instances are cached as a module-level singleton so a long-
running MCP server doesn't pay the FAISS-index + sentence-transformer
load cost on every call.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_ASSEMBLY_STEP: Any = None
_SYNTHESIS_STEP: Any = None
_LOAD_ERROR: str | None = None


def _workflow_dir() -> Path:
    """Locate the rag_e2e_synthesis workflow directory.

    Matches the resolution used by tests: walk up from this file to
    the repo root, then descend into the workflow directory.
    """
    here = Path(__file__).resolve()
    # mcp_surface/tools/synthesis.py → repo root is parents[3]
    return here.parents[3] / "composition" / "workflows" / "rag_e2e_synthesis"


def _load_steps() -> tuple[Any, Any] | tuple[None, None]:
    """Lazy-load and cache the assembly + synthesis step instances.

    Returns ``(assembly, synthesis)`` on success or ``(None, None)``
    when the load failed. The caller should check the module-level
    ``_LOAD_ERROR`` to surface the reason.
    """
    global _ASSEMBLY_STEP, _SYNTHESIS_STEP, _LOAD_ERROR
    if _ASSEMBLY_STEP is not None and _SYNTHESIS_STEP is not None:
        return _ASSEMBLY_STEP, _SYNTHESIS_STEP
    if _LOAD_ERROR is not None:
        return None, None

    try:
        from nanobrain.core.step import BaseStep

        wf_dir = _workflow_dir()
        assembly_yaml = wf_dir / "steps" / "synthesis_context_assembly.yml"
        synthesis_yaml = wf_dir / "steps" / "rag_synthesis.yml"

        if not assembly_yaml.is_file():
            _LOAD_ERROR = (
                f"Assembly step YAML not found at {assembly_yaml}. "
                "The rag_e2e_synthesis workflow must be present in this "
                "checkout for synthesize_query to work."
            )
            return None, None
        if not synthesis_yaml.is_file():
            _LOAD_ERROR = f"Synthesis step YAML not found at {synthesis_yaml}."
            return None, None

        _ASSEMBLY_STEP = BaseStep.from_config(str(assembly_yaml))
        _SYNTHESIS_STEP = BaseStep.from_config(str(synthesis_yaml))
        log.info("synthesize_query: loaded assembly + synthesis steps")
    except Exception as exc:
        _LOAD_ERROR = f"Failed to load synthesis pipeline: {type(exc).__name__}: {exc}"
        log.warning(_LOAD_ERROR)
        return None, None

    return _ASSEMBLY_STEP, _SYNTHESIS_STEP


async def synthesize_query(
    query: str,
    skip_pubmed: bool = False,
) -> dict:
    """Run the end-to-end RAG synthesis pipeline on a single query.

    Drives the two-step rag_e2e_synthesis workflow (assembly +
    synthesis) directly, bypassing the Composer. The assembly step
    runs domain-RAG semantic search, VIOLIN/BV-BRC tabular lookup,
    and PubMed publication harvesting concurrently; the synthesis
    step produces a Markdown answer with inline citations grounded
    in the retrieved data.

    Args:
        query: The scientist question. Must be non-empty.
        skip_pubmed: When True, skip the PubMed network branch
            (offline / sandboxed environments).

    Returns:
        On success: ``{"synthesis": "<markdown>", "retrieved": {
            "rag_chunks": <int>, "violin_mappings": <int>,
            "bvbrc_genomes": <int>, "publications": <int>}}``.
        On error: ``{"error": "<message>"}`` — a missing index file,
        broken LLM endpoint, or empty-retrieval gate failure all
        surface this way; the MCP client should display the message
        to the operator without retrying.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string"}

    assembly, synthesis = _load_steps()
    if assembly is None or synthesis is None:
        return {"error": _LOAD_ERROR or "synthesis pipeline not loaded"}

    # Override skip_pubmed if requested. We mutate the cached singleton
    # because the YAML default may differ from the per-call request and
    # rebuilding the step would reload the FAISS index.
    prior_skip = getattr(assembly, "_skip_pubmed", False)
    assembly._skip_pubmed = bool(skip_pubmed)
    try:
        bundle = await assembly.process({"query": query.strip()})
        result = await synthesis.process(bundle)
    except ValueError as exc:
        # Synthesizer's grounded-citation / size / empty-retrieval
        # gates raise ValueError. Surface the message verbatim — it
        # tells the operator which gate fired.
        return {"error": f"synthesis gate failed: {exc}"}
    except Exception as exc:
        return {"error": f"synthesis failed: {type(exc).__name__}: {exc}"}
    finally:
        assembly._skip_pubmed = prior_skip

    return {
        "synthesis": result.get("synthesis", ""),
        "retrieved": {
            "rag_chunks": len(bundle.get("rag_chunks", [])),
            "violin_mappings": len(bundle.get("violin_mappings", [])),
            "bvbrc_genomes": len(bundle.get("bvbrc_genomes", [])),
            "publications": len(bundle.get("publications", [])),
        },
    }


__all__ = ["synthesize_query"]
