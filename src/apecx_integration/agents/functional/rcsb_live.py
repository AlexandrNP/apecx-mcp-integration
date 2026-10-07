"""Live RCSB PDB structural search — authoritative, current, taxon-filtered.

The APECx Globus "aggregate" index (``e74bf12a``) is a PARTIAL, point-in-time harvest of
PDB (~27K of PDB's ~230K entries). Taxon-correct structures can therefore be genuinely
ABSENT from it — verified live 2026-10-07: the index held 4 Nipah PDB records while RCSB
holds 76, and NONE of the ~21 real G/F glycoprotein structures (2VSM, 3D11, 5EVM, 6T3F,
...) had ever been harvested in. No query change against the stale snapshot can surface
them; they must be sourced from the authoritative database directly.

This module queries the public RCSB Search + Data APIs, scoped to the organism's CURRENT
NCBI scientific name spelling(s). It stays organism-GROUNDED: the Search filter is an
exact match on ``rcsb_entity_source_organism.ncbi_scientific_name`` over the spellings the
caller already taxon-resolved (the same spellings the Globus facet pre-pass enumerates),
so no off-organism records leak in. This is a current, authoritative structural source —
NOT a free-text web search.

A network or API error raises :class:`RcsbLiveUnavailableError` so the caller degrades
LOUD to the Globus-only result rather than crashing or silently emptying.

Not subclassing ``functional._http.AsyncHttpClient``: that shared base is GET-JSON only and
treats an empty body as an error, whereas RCSB Search returns ``204 No Content`` for a
zero-result query and the Data API enrichment is a GraphQL POST. Keeping this client
self-contained avoids widening the shared base (used by the iedb/sifts/uniprot clients)
for two behaviours only RCSB needs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
_GRAPHQL_URL = "https://data.rcsb.org/graphql"
_DEFAULT_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_BACKOFF_BASE = 1.0


class RcsbLiveUnavailableError(RuntimeError):
    """RCSB Search/Data API unreachable or erroring after retries — caller degrades loud."""


@dataclass
class LiveStructure:
    """One RCSB structure with the metadata the structural MCP tool renders."""

    pdb_id: str
    title: str = ""
    resolution: float | None = None
    method: str = ""
    organisms: list[str] = field(default_factory=list)
    molecules: list[str] = field(default_factory=list)


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    """POST ``payload`` as JSON with retry/backoff on transient errors.

    Returns the raw response (callers inspect ``status_code`` — RCSB Search uses 204 for a
    zero-result query, which is NOT an error). Raises :class:`RcsbLiveUnavailableError`
    after exhausting retries on a network error or a 429/5xx.
    """
    last = ""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt >= _RETRY_ATTEMPTS:
                raise RcsbLiveUnavailableError(f"RCSB POST {url} failed: {last}") from exc
            await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            if attempt >= _RETRY_ATTEMPTS:
                raise RcsbLiveUnavailableError(f"RCSB POST {url} gave up: {last}")
            await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            continue
        return resp
    raise RcsbLiveUnavailableError(f"RCSB POST {url} gave up: {last}")


async def search_pdb_ids_by_organism_names(
    names: list[str], *, rows: int = 100, timeout: float = _DEFAULT_TIMEOUT
) -> list[str]:
    """PDB entry IDs whose source-organism scientific name is one of ``names`` (exact match).

    The exact-match ``in`` filter on ``ncbi_scientific_name`` IS the taxon-lock — only
    structures deposited under one of the caller-resolved spellings are returned, so the
    result cannot contain an off-organism structure. Returns ``[]`` for an empty ``names``
    or a zero-result query (RCSB 204). Raises :class:`RcsbLiveUnavailableError` on an API
    failure.
    """
    names = [n for n in (names or []) if n and n.strip()]
    if not names:
        return []
    payload = {
        "query": {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_entity_source_organism.ncbi_scientific_name",
                "operator": "in",
                "value": names,
            },
        },
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": rows},
            "results_content_type": ["experimental"],
        },
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await _post_json(client, _SEARCH_URL, payload)
    if resp.status_code == 204:
        return []
    if resp.is_error:
        raise RcsbLiveUnavailableError(f"RCSB Search HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise RcsbLiveUnavailableError(f"RCSB Search returned non-JSON: {exc}") from exc
    return [hit["identifier"] for hit in data.get("result_set", []) if hit.get("identifier")]


_GQL_TEMPLATE = (
    "{entries(entry_ids:[%s]){rcsb_id struct{title} "
    "rcsb_entry_info{resolution_combined experimental_method} "
    "polymer_entities{rcsb_polymer_entity{pdbx_description} "
    "rcsb_entity_source_organism{ncbi_scientific_name}}}}"
)


async def fetch_entry_metadata(
    ids: list[str], *, timeout: float = _DEFAULT_TIMEOUT
) -> dict[str, LiveStructure]:
    """Batch-fetch title/resolution/method/organisms/molecules for ``ids`` via the Data API.

    One GraphQL POST for the whole set. Returns ``{pdb_id: LiveStructure}``. Raises
    :class:`RcsbLiveUnavailableError` on an API failure.
    """
    if not ids:
        return {}
    quoted = ",".join(f'"{i}"' for i in ids)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await _post_json(client, _GRAPHQL_URL, {"query": _GQL_TEMPLATE % quoted})
    if resp.is_error:
        raise RcsbLiveUnavailableError(f"RCSB Data API HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        entries = (resp.json().get("data") or {}).get("entries") or []
    except ValueError as exc:
        raise RcsbLiveUnavailableError(f"RCSB Data API returned non-JSON: {exc}") from exc

    out: dict[str, LiveStructure] = {}
    for e in entries:
        if not e:
            continue
        pdb_id = e.get("rcsb_id") or ""
        resolution_combined = (e.get("rcsb_entry_info") or {}).get("resolution_combined") or []
        organisms: list[str] = []
        molecules: list[str] = []
        for pe in e.get("polymer_entities") or []:
            desc = (pe.get("rcsb_polymer_entity") or {}).get("pdbx_description")
            if desc and desc not in molecules:
                molecules.append(desc)
            for src in pe.get("rcsb_entity_source_organism") or []:
                name = src.get("ncbi_scientific_name")
                if name and name not in organisms:
                    organisms.append(name)
        out[pdb_id] = LiveStructure(
            pdb_id=pdb_id,
            title=(e.get("struct") or {}).get("title") or "",
            resolution=resolution_combined[0] if resolution_combined else None,
            method=(e.get("rcsb_entry_info") or {}).get("experimental_method") or "",
            organisms=organisms,
            molecules=molecules,
        )
    return out


async def live_pdb_structures_for_organisms(
    names: list[str], *, rows: int = 100, timeout: float = _DEFAULT_TIMEOUT
) -> list[LiveStructure]:
    """Authoritative PDB structures for the taxon-resolved organism ``names``.

    Searches by scientific name, then enriches with title/resolution/molecules. Order
    follows RCSB relevance. Raises :class:`RcsbLiveUnavailableError` on an API failure;
    returns ``[]`` when the organism genuinely has no experimental PDB structures.
    """
    ids = await search_pdb_ids_by_organism_names(names, rows=rows, timeout=timeout)
    if not ids:
        return []
    meta = await fetch_entry_metadata(ids, timeout=timeout)
    return [meta.get(i, LiveStructure(pdb_id=i)) for i in ids]


__all__ = [
    "LiveStructure",
    "RcsbLiveUnavailableError",
    "fetch_entry_metadata",
    "live_pdb_structures_for_organisms",
    "search_pdb_ids_by_organism_names",
]
