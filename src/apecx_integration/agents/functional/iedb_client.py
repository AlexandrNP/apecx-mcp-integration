"""IEDB client — known B/T-cell epitopes for an antigen (E3-3.3, bonus).

GET ``query-api.iedb.org/epitope_search`` (an IEDB PostgREST surface). The antigen is
selected by UniProt accession through the ``parent_source_antigen_iris`` ARRAY column,
which requires PostgREST containment syntax: ``cs.{UNIPROT:<acc>}`` (curly-brace array
literal). The singular ``eq.`` form errors against an array column — the ``cs.{}`` form is
load-bearing and pinned by a test (a schema change must FAIL LOUD, not return ``[]``).

The view returns each epitope's ``linear_sequence`` (no explicit positions); spans in
UNIPROT coordinates are derived downstream by locating the linear sequence inside the
UniProt canonical sequence (see ``residue_annotation.locate_epitope_spans``).

Cached by accession with a TTL (IEDB is a growing dataset, CC-4).
"""

from __future__ import annotations

import logging
from typing import Any

from apecx_integration.agents.functional import _cache
from apecx_integration.agents.functional._http import AsyncHttpClient, HttpClientError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://query-api.iedb.org"
_CACHE_SUBDIR = "iedb"
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600.0  # one week


def containment_param(accession: str) -> str:
    """Return the PostgREST array-containment filter value for ``accession``.

    The literal ``cs.{UNIPROT:<acc>}`` form is load-bearing (singular ``eq.`` errors on the
    array column). Exposed as a function so a regression test can pin the exact syntax.
    """
    return f"cs.{{UNIPROT:{accession}}}"


class IedbClient(AsyncHttpClient):
    """Async client for the IEDB query-api epitope_search endpoint."""

    _label = "IEDB"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url=base_url, **kwargs)
        self._ttl = ttl_seconds
        self._mem: dict[str, list[dict[str, Any]]] = {}

    async def search_epitopes(self, accession: str) -> list[dict[str, Any]]:
        """Return the epitopes whose parent source antigen is ``UNIPROT:<accession>``.

        Each item is ``{linear_sequence, structure_type, pdb_ids, parent_iris}``. Returns
        ``[]`` when the antigen has no IEDB epitopes (a genuine, named absence handled by
        the caller — NOT a silent empty). Cached in-process + on disk (TTL). Raises on a
        non-404 network failure (caller degrades loud).
        """
        if accession in self._mem:
            return self._mem[accession]

        path = _cache.cache_path(_CACHE_SUBDIR, accession)
        cached = _cache.read_json(path, ttl_seconds=self._ttl)
        if cached is not None:
            self._mem[accession] = cached
            return cached

        params = {
            "parent_source_antigen_iris": containment_param(accession),
            "select": "linear_sequence,structure_type,pdb_ids,parent_source_antigen_iris",
        }
        try:
            rows = await self._get_json("/epitope_search", params=params)
        except HttpClientError as exc:
            if "404" in str(exc):
                _cache.write_json(path, [])
                self._mem[accession] = []
                return []
            raise

        epitopes = self._parse(rows)
        _cache.write_json(path, epitopes)
        self._mem[accession] = epitopes
        return epitopes

    @staticmethod
    def _parse(rows: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            seq = row.get("linear_sequence")
            if not isinstance(seq, str) or not seq:
                continue  # non-linear (conformational) epitopes carry no locatable sequence
            out.append(
                {
                    "linear_sequence": seq,
                    "structure_type": row.get("structure_type"),
                    "pdb_ids": row.get("pdb_ids") or [],
                    "parent_iris": row.get("parent_source_antigen_iris") or [],
                }
            )
        return out


__all__ = ["IedbClient", "DEFAULT_BASE_URL", "containment_param"]
