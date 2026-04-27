"""violin_bvbrc — VIOLIN/BV-BRC entity extraction + synonym search.

Migrated from ``apecx_db_integration.agent`` (commit b54e571,
post-cluster-AR fix). The post-fix shape is preserved verbatim:
``MAX_CANDIDATES_PER_CATEGORY`` constant + per-category similarity
filter + ``logger.warning`` on truncation events.

Public API:
  - ``extract_entities_llm(query)`` — LLM-driven entity extraction.
  - ``consolidated_synonym_search(query, dfs=None, ...)`` — match
    extracted entities against VIOLIN candidate terms.
  - ``get_candidate_terms(dfs=None)`` — collect unique candidates
    per category from VIOLIN dataframes.
  - ``filter_candidates_by_similarity(query, candidates, max=100)``
    — string-similarity per-category filter.
  - ``MAX_CANDIDATES_PER_CATEGORY`` — single-line review point for
    the truncation cap (was a magic number ``[:100]`` pre-AR fix).

For local-LLM deployments, set ``APECX_LLM_BASE_URL`` to an Ollama
endpoint (e.g. ``http://localhost:11434/v1``), ``APECX_LLM_MODEL``
to a model name (e.g. ``mistral-nemo:latest``), and
``APECX_LLM_API_KEY`` to any non-empty placeholder
(``EMPTY`` / ``unused`` are conventional). The agent honors those
env vars via the ``_build_chat_llm`` factory.
"""

from apecx_integration.agents.violin_bvbrc.agent import (
    MAX_CANDIDATES_PER_CATEGORY,
    _build_chat_llm,
    _get_dfs,
    consolidated_synonym_search,
    enrich_matches_with_database_data,
    enrich_query_with_llm_synonyms,
    extract_entities_llm,
    filter_candidates_by_similarity,
    get_candidate_terms,
    get_llm_for_entity_extraction,
)

__all__ = [
    "MAX_CANDIDATES_PER_CATEGORY",
    "_build_chat_llm",
    "_get_dfs",
    "consolidated_synonym_search",
    "enrich_matches_with_database_data",
    "enrich_query_with_llm_synonyms",
    "extract_entities_llm",
    "filter_candidates_by_similarity",
    "get_candidate_terms",
    "get_llm_for_entity_extraction",
]
