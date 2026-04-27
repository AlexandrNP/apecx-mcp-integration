"""apecx_integration.agents — port of agent code formerly housed in
sibling repos (apecx-db-integration, apecx-rag).

Submodules:
  - ``violin_bvbrc``: VIOLIN/BV-BRC entity extraction + synonym search.
    Migrated from ``apecx_db_integration.agent`` per user directive
    2026-04-27 ("apecx-rag and apecx-db-integration functionality
    should be merged in apecx-mcp-integration").
  - ``rag_synthesis``: RAG-augmented LLM synthesis. Migrated from
    the apecx-rag prototype's LangGraph agent team.

Migration approach: copy-then-refactor (user-authorized 2026-04-27).
Day 1 lands the verbatim copies; later days refactor the agents into
nanobrain-style configurable agents with YAML config + local-LLM
support and remove the cross-repo dependency.
"""
