"""Shared HITL gate for MCP tools that resolve user-supplied entity names.

The harmonized_search workflow's design contract is that when a user
surface form resolves to multiple distinct canonical IRIs (RSV → 6
candidates: human RSV / bovine / Rous sarcoma / etc.), the LLM gets a
*paused envelope* with the candidate list and stops — control returns
to the user-facing LLM to ask the user which they meant.

Several pre-harmonized_search MCP tools (``query_pathogens``,
``query_vaccines``, ``query_genes``, ``query_bvbrc_genomes``,
``get_vaccine_pathogen_genes``, ``resolve_entity``) called
:func:`apecx_integration.synonym_dictionary.lookup.lookup_entity`
directly and silently picked the first match — the exact silent
mis-attribution failure mode harmonized_search was built to prevent.

This module is the single source of truth for the HITL gate every
entity-named MCP tool now routes through. The gate's output shape is
designed to match the harmonized_search workflow's paused-envelope
shape exactly, so a user-facing LLM behaves identically regardless of
which tool surfaces the ambiguity.

Usage from an MCP tool::

    from apecx_integration.mcp_surface.tools._hitl_gate import (
        resolve_with_hitl_gate,
    )

    gate = resolve_with_hitl_gate(
        term=search_term,
        entity_type=EntityType.PATHOGEN,
        param_name="search_term",
        tool_name="query_bvbrc_genomes",
    )
    if gate["status"] == "paused_awaiting_disambiguation":
        return gate  # surface candidates to the user
    # proceed with gate["lookup_result"] / gate["ncbi_taxonomy_id"]

A tool that takes no entity name (empty ``term``) should not call the
gate — there's nothing to disambiguate.

A tool that accepts an IRI as the term (after disambiguation round-2)
passes that IRI through as-is; the gate detects it and short-circuits
the ambiguity check (an IRI is by construction unambiguous).
"""

from __future__ import annotations

from typing import Any

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.lookup import (
    LookupResult,
    detect_ambiguity,
)


def _ncbi_taxon_id(iri: str | None) -> int | None:
    """Extract the integer NCBI taxon ID from an OBO IRI tail.

    Mirrors the helper in harmonized_search_execute_step + the
    database_tools._ncbi_taxon_id helper; duplicated rather than imported
    to keep this gate self-contained.
    """
    if not isinstance(iri, str) or not iri:
        return None
    suffix = iri.rsplit("/", 1)[-1].split("_", 1)[-1]
    try:
        return int(suffix)
    except (TypeError, ValueError):
        return None


def _build_paused_envelope(
    *,
    term: str,
    candidates: list[dict[str, Any]],
    param_name: str,
    tool_name: str,
) -> dict[str, Any]:
    """Build the paused-envelope shape — identical to harmonized_search's."""
    md_lines = [
        f"### Term `{term}` is ambiguous (tool `{tool_name}`)",
        "",
        (
            f"The synonym dictionary resolves `{term}` to {len(candidates)} "
            f"distinct canonical entries. The tool has NOT been executed — "
            f"running it across all candidate taxa would lump heterogeneous "
            f"biology together. Pick one:"
        ),
        "",
    ]
    for c in candidates:
        md_lines.append(f"  - `{c['canonical_iri']}` — {c['canonical_label']!r}")
    md_lines += [
        "",
        (
            f"Re-call `{tool_name}` with the chosen canonical IRI as "
            f"`{param_name}` (the resolver short-circuits IRI inputs via "
            f"`path=fast`)."
        ),
    ]
    return {
        "status": "paused_awaiting_disambiguation",
        "markdown": "\n".join(md_lines),
        "next_action": {
            "kind": "re-invoke_with_chosen_iri",
            "tool": tool_name,
            "param_name": param_name,
            "options": [c["canonical_iri"] for c in candidates],
        },
        "candidates": candidates,
        "tool": tool_name,
        "term": term,
    }


def resolve_with_hitl_gate(
    *,
    term: str,
    entity_type: EntityType | None,
    param_name: str,
    tool_name: str,
) -> dict[str, Any]:
    """Resolve ``term`` with structural HITL gating.

    Returns one of three shapes:

    - **paused** (``status == "paused_awaiting_disambiguation"``) —
      surface candidates to the user and stop. Tool MUST return this
      dict directly to its caller without touching the data layer.

    - **resolved** (``status == "resolved"``) — the term mapped to a
      single canonical entry. The tool proceeds with
      ``lookup_result`` and ``ncbi_taxonomy_id``.

    - **bypass** (``status == "bypass"``) — ``term`` is empty; no
      resolution attempted. The tool proceeds with no entity filter.

    The gate is a *structural* guarantee: a tool that routes its
    user-provided entity name through this helper cannot silently
    mis-attribute an ambiguous term, regardless of LLM cooperation.
    """
    if not term or not term.strip():
        return {"status": "bypass"}

    primary, candidates = detect_ambiguity(term, entity_type=entity_type)

    if len(candidates) > 1:
        return _build_paused_envelope(
            term=term,
            candidates=candidates,
            param_name=param_name,
            tool_name=tool_name,
        )

    return {
        "status": "resolved",
        "lookup_result": primary,
        "ncbi_taxonomy_id": _ncbi_taxon_id(primary.canonical_iri),
        "resolution_meta": _lookup_result_to_meta(primary),
    }


def _lookup_result_to_meta(lr: LookupResult) -> dict[str, Any]:
    """Project the LookupResult to a serializable resolution-metadata dict."""
    return {
        "path": lr.path,
        "canonical_iri": lr.canonical_iri,
        "canonical_label": lr.canonical_label,
        "canonical_ontology": lr.canonical_ontology,
        "confidence": lr.confidence,
        "resolution_status": lr.resolution_status.value,
        "evidence": lr.evidence,
    }


__all__ = [
    "resolve_with_hitl_gate",
]
