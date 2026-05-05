"""MCP tool — query the APECx Globus Search index.

The harvester (apecx-harvesters sibling) is a stand-alone process
that runs once and updates two outputs: the APECx synonym dictionary
(consumed via ``apecx_integration.synonym_dictionary``) and the
**Globus Search index** (consumed via this tool). This module sits
at the **ingest** boundary — it queries the index but never writes
to it. Harvester code is explicitly out of scope.

When to use ``query_globus_search`` vs. the existing tools:

  - Use ``query_globus_search`` for FREE-TEXT exploration of the
    full harvested corpus (PubMed papers, PDB structures, DataCite
    records). Returns ranked records keyed by subject.
  - Use ``query_pathogens`` / ``query_vaccines`` / etc. for
    structured lookup against the VIOLIN + BV-BRC tables (those
    sources are NOT in the Globus index — they are joined offline
    into the synonym dictionary instead).
  - Use ``synthesize_query`` for "ask a question, get a Markdown
    answer" — the synthesis pipeline now folds Globus results into
    the retrieval bundle alongside FAISS / VIOLIN/BV-BRC / PubMed.
"""

from __future__ import annotations

from apecx_integration.agents.globus_search import (
    GlobusSearchUnavailableError,
)
from apecx_integration.agents.globus_search import (
    search as _search,
)


async def query_globus_search(
    query: str,
    max_results: int = 20,
    offset: int = 0,
) -> dict:
    """Query the APECx Globus Search index for harvested records.

    Args:
        query: Free-text query passed verbatim to Globus Search.
            Globus uses Lucene-style query syntax for advanced use
            (e.g. ``"vaccine" AND "alphavirus"``); a plain phrase is
            treated as a multi-keyword OR query.
        max_results: Hard cap on returned hits. Globus enforces a max
            of 100; values above are clamped server-side.
        offset: Pagination offset for fetching subsequent pages.

    Returns:
        On success: ``{"results": [{"subject", "content", "score"},
        ...], "count": N, "query": "<echo>"}`` where ``subject`` is
        the harvester's unique identifier for the record (typically a
        DOI, PMID, or PDB accession) and ``content`` is the indexed
        payload (shape varies by source — PubMed records carry title,
        abstract, authors; PDB records carry structure metadata).

        On error: ``{"error": "<message>", "query": "<echo>"}`` —
        missing globus_sdk, network failure, or invalid index UUID
        all surface this way without raising.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string", "query": query}

    try:
        hits = _search(
            query,
            max_results=int(max_results),
            offset=int(offset),
        )
    except GlobusSearchUnavailableError as exc:
        return {"error": str(exc), "query": query}
    except Exception as exc:
        # Defensive: any non-GlobusSearchUnavailableError fault gets
        # marshaled rather than raised through the MCP transport.
        return {
            "error": f"unexpected error: {type(exc).__name__}: {exc}",
            "query": query,
        }

    return {
        "results": hits,
        "count": len(hits),
        "query": query,
    }


__all__ = ["query_globus_search"]
