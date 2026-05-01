"""MCP tool: resolve_canonical_entity.

Exposes Stage 2 fast-path lookup as a scientist-facing MCP tool.

Visibility guarantee (analysis doc §0.1, §6.2.1):
  This tool ALWAYS tells the caller which path was taken (fast vs slow
  vs miss) and at what confidence.  Silent routing would recreate the
  partial-retrieval problem the whole Stage 1/Stage 2 work is meant to
  prevent.  Do not simplify the return shape to remove path/confidence.
"""

from __future__ import annotations

import os
from pathlib import Path

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.loader import configure_dictionary_path
from apecx_integration.synonym_dictionary.lookup import LookupResult, lookup_entity

# Initialize the dictionary path from environment at import time.
# APECX_SYNONYM_DICT_PATH can point at a pre-built dictionary.sqlite.
# When absent the fast path is disabled and the tool falls back to slow-path.
_dict_path_env = os.environ.get("APECX_SYNONYM_DICT_PATH")
if _dict_path_env:
    configure_dictionary_path(Path(_dict_path_env))

_ENTITY_TYPE_MAP: dict[str, EntityType] = {
    "pathogen": EntityType.PATHOGEN,
    "vaccine": EntityType.VACCINE,
    "disease": EntityType.DISEASE,
    "gene": EntityType.GENE,
}


def _result_to_dict(result: LookupResult) -> dict:
    return {
        "surface_form": result.surface_form,
        "resolution_path": result.path,
        "canonical_iri": result.canonical_iri,
        "canonical_label": result.canonical_label,
        "canonical_ontology": result.canonical_ontology,
        "confidence": result.confidence,
        "resolution_status": result.resolution_status.value,
        "synonyms": list(result.synonyms),
        "evidence": result.evidence,
    }


async def resolve_canonical_entity(
    name: str,
    entity_type: str = "",
) -> dict:
    """Resolve a biomedical entity name to its canonical ontology IRI.

    This is the Stage 2 fast-path tool for ontology-based entity resolution.
    It first checks the pre-built synonym dictionary (fast, O(1), highly
    accurate for well-curated entities), then falls back to substring
    matching against the raw database (slow, approximate).

    The response always includes ``resolution_path`` (one of "fast", "slow",
    "miss") and ``confidence`` so the caller knows how much to trust the
    result.  A "fast" hit is dictionary-backed and highly reliable.  A
    "slow" hit is substring-based and should be treated as approximate.
    A "miss" means no match was found on either path.

    Parameters
    ----------
    name:
        The entity name to resolve.  Can be a formal name, abbreviation,
        synonym, or a full OBO IRI.  Examples: "EEEV", "Chikungunya virus",
        "H1N1", "HIV-1", "http://purl.obolibrary.org/obo/NCBITaxon_37124".
    entity_type:
        Optional filter: one of "pathogen", "vaccine", "disease", "gene".
        When empty, search across all entity types.

    Returns
    -------
    A dict with:

    - ``surface_form``: the input name
    - ``resolution_path``: "fast" | "slow" | "miss"
    - ``canonical_iri``: OBO IRI (e.g. "http://purl.obolibrary.org/obo/NCBITaxon_37124")
      or null when unresolved
    - ``canonical_label``: preferred label from the ontology, or null
    - ``canonical_ontology``: ontology name (e.g. "ncbitaxon", "vo"), or null
    - ``confidence``: float 0.0-1.0 (1.0 = id-anchored, ~0.9 = OLS exact,
      ~0.5-0.7 = OLS fuzzy, ~0.3 = substring, 0.0 = miss)
    - ``resolution_status``: "id_anchored" | "ols_exact" | "ols_fuzzy" | "unresolved"
    - ``synonyms``: list of known surface forms for the resolved entity
    - ``evidence``: human-readable explanation of how the resolution was made
    """
    if not name or not name.strip():
        return {
            "error": "name is required",
            "resolution_path": "miss",
        }

    etype: EntityType | None = None
    if entity_type:
        etype = _ENTITY_TYPE_MAP.get(entity_type.lower().strip())
        if etype is None:
            return {
                "error": (
                    f"unknown entity_type {entity_type!r}; "
                    f"valid values: {', '.join(_ENTITY_TYPE_MAP)}"
                ),
                "resolution_path": "miss",
            }

    result = lookup_entity(name.strip(), entity_type=etype)
    return _result_to_dict(result)


__all__ = ["resolve_canonical_entity"]
