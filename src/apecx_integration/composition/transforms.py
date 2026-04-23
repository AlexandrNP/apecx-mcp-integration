"""Sample data-unit transforms for nanobrain TransformLink.

Each function here is pure (``(input_dict) -> output_dict``), importable
via a dotted path, and safe to wire into a ``TransformLink`` YAML
entry's ``transform_function`` field.

Example workflow YAML::

    links:
      rename_step1_to_step3a:
        class: "nanobrain.core.link.TransformLink"
        config:
          link_type: "transform"
          source: "entity_extraction.entity_candidates_output"
          target: "synonym_cache_lookup.query_terms_input"
          transform_function: "apecx_integration.composition.transforms.entities_to_query_terms"

Scope note
----------
These transforms cover the two cross-step key-shape bridges present in
the T01 vertical slice (workflow spec §3.1). Today, T01 handles those
bridges inside the Step bodies (``EntityExtractionStep`` emits
``query_terms`` alongside ``entities``; ``VerifiedSynonymWritebackStep``
accepts both ``approved_mappings`` and ``llm_proposals``). Those
in-Step bridges are NOT being reverted — they're tested and shipped.

This module exists so FUTURE workflows (composer-generated or
hand-authored) can choose the link-side pattern when it's cleaner,
now that the framework supports it. Having both patterns available
with clear examples of each is healthier than forcing one.
"""

from __future__ import annotations

from typing import Any


def entities_to_query_terms(input_data: dict[str, Any]) -> dict[str, Any]:
    """Reshape ``EntityExtractionStep`` output for ``SynonymCacheLookupStep``.

    Accepts:
      - ``query_terms`` key (already flattened) — passed through.
      - ``entities`` key (list of {name, type, confidence}) — names
        extracted into a list.

    Returns ``{"query_terms": [...]}``. Never raises on missing keys;
    empty input → empty output. The wrapping Step enforces its own
    shape requirements; this transform just renames/flattens.
    """
    if "query_terms" in input_data and isinstance(input_data["query_terms"], list):
        return {"query_terms": list(input_data["query_terms"])}

    entities = input_data.get("entities", [])
    names = [e["name"] for e in entities if isinstance(e, dict) and "name" in e]
    return {"query_terms": names}


def llm_proposals_to_approved_mappings(input_data: dict[str, Any]) -> dict[str, Any]:
    """Reshape ``ApprovalStep`` passthrough output for
    ``VerifiedSynonymWritebackStep``.

    Accepts ``llm_proposals`` (list of {query_entity, synonym, score}).
    Renames keys:
      - ``query_entity`` → ``query_term``
      - ``synonym``      → ``canonical_term``
      - ``score``        → ``confidence``

    Passes reviewer metadata (``source_run_id``, ``comment``) through
    if present; sets them to ``None`` otherwise so the writeback step's
    payload shape is uniform.
    """
    proposals = input_data.get("llm_proposals", [])
    mappings = []
    for p in proposals:
        mappings.append({
            "query_term": p["query_entity"],
            "canonical_term": p["synonym"],
            "confidence": float(p.get("score", 1.0)),
            "source_run_id": p.get("source_run_id"),
            "comment": p.get("comment"),
        })
    return {"approved_mappings": mappings}


__all__ = [
    "entities_to_query_terms",
    "llm_proposals_to_approved_mappings",
]
