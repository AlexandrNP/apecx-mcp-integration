"""Globus Search index client — read-only access to the APECx corpus.

The harvester populates the index (subject-keyed records of harvested
PubMed, PDB, DataCite, etc.); we only consume from it. The index is
public — no auth tokens required for queries.

Configuration:

  - ``APECX_GLOBUS_SEARCH_INDEX_UUID`` env var overrides the default
    index UUID. The default points at the public APECx index.
  - ``APECX_GLOBUS_SEARCH_DISABLED=1`` short-circuits all queries to
    return an empty list (offline test environments / sandboxed runs).

Failure shape: ``GlobusSearchUnavailableError`` for misconfiguration
or network errors. The synthesis pipeline catches this via
``asyncio.gather(return_exceptions=True)`` and degrades the Globus
branch to an empty list — same contract as the PubMed branch.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Default APECx Globus Search index UUID. Public, no auth required.
# Source of truth: apecx-mcp/src/apecx_mcp/harvesters.py:151. Kept in
# sync manually because the apecx-mcp repo doesn't export this as a
# library symbol. If the index UUID rotates, update both.
APECX_GLOBUS_INDEX_UUID = "e74bf12a-d0dd-4d19-a965-03f4936db851"


class GlobusSearchUnavailableError(RuntimeError):
    """Raised when the Globus Search query cannot be completed.

    Wraps either an import-time error (globus_sdk not installed),
    a configuration error (invalid index UUID), or a network/HTTP
    error from the SDK. The synthesis pipeline catches this and
    degrades the Globus branch to an empty list.
    """


def _resolve_index_uuid() -> str:
    return os.environ.get(
        "APECX_GLOBUS_SEARCH_INDEX_UUID",
        APECX_GLOBUS_INDEX_UUID,
    )


def _is_disabled() -> bool:
    return os.environ.get("APECX_GLOBUS_SEARCH_DISABLED") == "1"


def search(
    query: str,
    *,
    max_results: int = 20,
    offset: int = 0,
    filters: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Query the APECx Globus Search index.

    Args:
        query: Free-text search query. Empty / whitespace-only short-
            circuits to ``[]``.
        max_results: Hard cap on returned hits. Globus enforces a max
            of 100 per request; values above that are clamped.
        offset: Starting offset for pagination.
        filters: Optional list of Globus Search filter clauses (e.g.
            ``[{"type": "match_any", "field_name": "publisher.name",
            "values": ["RCSB PDB"]}]``). When provided, the clauses are
            attached to the post_search payload AND ``advanced: true`` is
            set (structured field filters require advanced mode). This is
            how callers restrict the aggregate index to a single logical
            source (PDB vs EMDB) by ``publisher.name`` — the verified
            server-side discriminator. ``None`` (default) preserves the
            original free-text-only behavior exactly.

    Returns:
        List of normalized hit dicts with keys ``subject`` (the unique
        harvester record identifier), ``content`` (the indexed payload
        — varies by source), ``score`` (relevance, ``None`` when the
        index doesn't surface scores).

    Raises:
        :class:`GlobusSearchUnavailableError`: when the SDK is missing,
            the index UUID is invalid, or the query fails for network
            or auth reasons. Callers (notably the synthesis pipeline's
            ``asyncio.gather(return_exceptions=True)``) catch this and
            degrade gracefully.
    """
    if not isinstance(query, str) or not query.strip():
        return []

    if _is_disabled():
        log.debug("globus_search.search: APECX_GLOBUS_SEARCH_DISABLED=1; returning []")
        return []

    try:
        from globus_sdk import SearchClient
    except ImportError as exc:
        raise GlobusSearchUnavailableError(
            "globus_sdk is not installed; run `pip install globus-sdk`"
        ) from exc

    index_uuid = _resolve_index_uuid()
    capped = max(1, min(int(max_results), 100))

    payload: dict[str, Any] = {"q": query.strip(), "limit": capped, "offset": int(offset)}
    if filters:
        # Structured field filters require Globus "advanced" query mode;
        # without advanced=true the filters are silently ignored and the
        # query degrades to free-text — exactly the silent-failure shape
        # we refuse. Set it explicitly whenever filters are present.
        payload["filters"] = filters
        payload["advanced"] = True

    try:
        client = SearchClient()
        result = client.post_search(index_uuid, payload)
    except Exception as exc:
        raise GlobusSearchUnavailableError(
            f"Globus Search query failed against index {index_uuid}: {type(exc).__name__}: {exc}"
        ) from exc

    hits: list[dict[str, Any]] = []
    for entry in result.get("gmeta", []):
        subject = entry.get("subject", "")
        entries = entry.get("entries", []) or []
        # GMetaResult bundles N entries per subject; the first is the
        # primary record. Score comes from entry-level metadata when
        # the index publishes ``@datatype`` boost values; missing →
        # None (synthesizer treats absent score as "low confidence").
        content = entries[0].get("content", {}) if entries else {}
        score = entries[0].get("entry_id") if entries else None  # placeholder
        hits.append(
            {
                "subject": subject,
                "content": content,
                "score": None if isinstance(score, str) else score,
            }
        )

    log.info(
        "globus_search: q=%.80r → %d hits (total=%d, index=%s)",
        query,
        len(hits),
        result.get("total", 0),
        index_uuid,
    )
    return hits


def facet(
    field_name: str,
    query: str,
    *,
    filters: list[dict[str, Any]] | None = None,
    size: int = 100,
) -> list[tuple[str, int]]:
    """Enumerate the distinct values of one indexed field (a Globus terms facet).

    Used to discover the real spelling set of a nested field — notably
    ``pdb.polymer_entities.scientific_name``, whose organism values carry
    strain/case variants ("Chikungunya virus", "CHIKUNGUNYA VIRUS",
    "Chikungunya virus strain S27-African prototype") that an EXACT,
    case-sensitive ``match_any`` filter would otherwise under-recall. A facet
    pre-pass scoped by a species term enumerates every variant present so the
    caller can build a complete ``match_any`` value set.

    Args:
        field_name: The (possibly nested) field to facet on.
        query: Free-text scope for the records aggregated into the facet. Empty /
            whitespace-only short-circuits to ``[]``.
        filters: Optional structured filter clauses applied alongside the facet
            (e.g. ``publisher.name = "RCSB PDB"``). Facets require ``advanced``
            mode, which is set unconditionally here.
        size: Bucket cap (Globus enforces its own server-side max).

    Returns:
        ``[(value, count), ...]`` bucket list, highest-count first as the index
        returns it. ``[]`` when the field has no values in scope.

    Raises:
        :class:`GlobusSearchUnavailableError`: same failure shape as :func:`search`
            — SDK missing, bad index UUID, or a network/HTTP error.
    """
    if not isinstance(query, str) or not query.strip():
        return []

    if _is_disabled():
        log.debug("globus_search.facet: APECX_GLOBUS_SEARCH_DISABLED=1; returning []")
        return []

    try:
        from globus_sdk import SearchClient
    except ImportError as exc:
        raise GlobusSearchUnavailableError(
            "globus_sdk is not installed; run `pip install globus-sdk`"
        ) from exc

    index_uuid = _resolve_index_uuid()
    payload: dict[str, Any] = {
        "q": query.strip(),
        "limit": 0,
        "advanced": True,
        "facets": [{"name": "f", "type": "terms", "field_name": field_name, "size": int(size)}],
    }
    if filters:
        payload["filters"] = filters

    try:
        client = SearchClient()
        result = client.post_search(index_uuid, payload)
    except Exception as exc:
        raise GlobusSearchUnavailableError(
            f"Globus Search facet on {field_name!r} failed against index "
            f"{index_uuid}: {type(exc).__name__}: {exc}"
        ) from exc

    buckets: list[tuple[str, int]] = []
    for facet_result in result.get("facet_results", []) or []:
        for bucket in facet_result.get("buckets", []) or []:
            value = bucket.get("value")
            if isinstance(value, str):
                buckets.append((value, int(bucket.get("count", 0))))
    log.info(
        "globus_search.facet: field=%s q=%.60r → %d buckets (index=%s)",
        field_name,
        query,
        len(buckets),
        index_uuid,
    )
    return buckets
