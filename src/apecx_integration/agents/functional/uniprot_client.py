"""UniProt client — residue-level sequence features + canonical sequence (E3-3.2).

GET ``rest.uniprot.org/uniprotkb/{accession}.json`` with a residue-feature field mask.
Returns the residue-level features (glycosylation, disulfide, active/binding sites,
domains) each with ``start``/``end`` in UNIPROT coordinates, plus the canonical sequence
(needed to locate IEDB linear epitopes in the same UNIPROT frame).

Cached by accession (CC-4). UniProt is a moving target, so the cached payload records the
UniProt release + the query date for provenance; the cache is keyed by accession and
re-served byte-stable across runs (the release stamp tells a reader which snapshot it is).
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from apecx_integration.agents.functional import _cache
from apecx_integration.agents.functional._http import AsyncHttpClient, HttpClientError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://rest.uniprot.org"
_CACHE_SUBDIR = "uniprot"

# Residue-level feature field mask (verified live 2026-06-13 against Q1H8W5).
FEATURE_FIELDS = "ft_carbohyd,ft_binding,ft_disulfid,ft_act_site,ft_site,ft_domain"


class UniProtClient(AsyncHttpClient):
    """Async client for the UniProtKB REST API (residue features + sequence)."""

    _label = "UniProt"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs: Any) -> None:
        super().__init__(base_url=base_url, **kwargs)
        self._mem: dict[str, dict[str, Any] | None] = {}

    async def get_entry(self, accession: str) -> dict[str, Any] | None:
        """Return ``{accession, release, query_date, sequence, features:[...]}`` or ``None``.

        Each feature is ``{type, start, end, description}`` in UNIPROT coords. ``None`` when
        the accession is unknown (404). Cached in-process + on disk by accession. Raises on
        a non-404 network failure (caller degrades loud).
        """
        if accession in self._mem:
            return self._mem[accession]

        path = _cache.cache_path(_CACHE_SUBDIR, accession)
        cached = _cache.read_json(path)
        if cached is not None:
            result = cached or None
            self._mem[accession] = result
            return result

        params = {"fields": f"{FEATURE_FIELDS},sequence"}
        try:
            resp_json, headers = await self._get_json_and_headers(
                f"/uniprotkb/{accession}.json", params=params
            )
        except HttpClientError as exc:
            if "404" in str(exc):
                _cache.write_json(path, {})
                self._mem[accession] = None
                return None
            raise

        entry = self._parse(resp_json, accession, headers)
        _cache.write_json(path, entry)
        self._mem[accession] = entry
        return entry

    @staticmethod
    def _parse(
        body: dict[str, Any], accession: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        features: list[dict[str, Any]] = []
        for f in body.get("features", []) or []:
            loc = f.get("location", {}) or {}
            start = (loc.get("start", {}) or {}).get("value")
            end = (loc.get("end", {}) or {}).get("value")
            if start is None or end is None:
                continue
            features.append(
                {
                    "type": f.get("type"),
                    "start": int(start),
                    "end": int(end),
                    "description": f.get("description") or "",
                }
            )
        # The UniProt release lives in the x-uniprot-release header (e.g. "2026_02"),
        # not the field-masked body. Record it for provenance (CC-4).
        release = (headers or {}).get("x-uniprot-release") or body.get("entryType", "")
        return {
            "accession": accession,
            "release": release,
            "query_date": _dt.date.today().isoformat(),
            "sequence": (body.get("sequence", {}) or {}).get("value", ""),
            "features": features,
        }


__all__ = ["UniProtClient", "DEFAULT_BASE_URL", "FEATURE_FIELDS"]
