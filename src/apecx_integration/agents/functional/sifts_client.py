"""SIFTS client — PDB→UniProt accession + author-numbering residue bridge (E3-3.1).

The PDBe SIFTS service (``ebi.ac.uk/pdbe/api/mappings/uniprot/{pdb}``) returns, per
chain, the residue mapping between PDB coordinates and UniProt coordinates. Two numbering
frames are present per mapping segment:

- ``residue_number`` — PDBe label numbering.
- ``author_residue_number`` — the AUTHOR (auth_seq_id) numbering.

The PyMOL job (``docker/pymol/_pymol_job.py``) emits ``at.resi`` = ``auth_seq_id``, so the
candidate epitope residues in ``structural_reasoning.exposed_residues[].resi`` are in the
AUTHOR frame. The bridge MUST therefore use ``author_residue_number`` — NOT
``residue_number`` (and NOT RCSB ``aligned_regions``, which is label/entity numbering and
silently off by hundreds). For 2XFB chain A this gives author 1-391 ↔ UniProt 810-1200
(a +809 offset); chain B differs (+261), so the bridge is strictly per-chain, per-segment.

Mappings are cached by PDB id indefinitely (PDB/SIFTS are immutable, CC-4).
"""

from __future__ import annotations

import logging
from typing import Any

from apecx_integration.agents.functional import _cache
from apecx_integration.agents.functional._http import AsyncHttpClient, HttpClientError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/pdbe/api"
_CACHE_SUBDIR = "sifts"


class SiftsClient(AsyncHttpClient):
    """Async client for the PDBe SIFTS UniProt-mappings API."""

    _label = "SIFTS"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, **kwargs: Any) -> None:
        super().__init__(base_url=base_url, **kwargs)
        self._mem: dict[str, dict[str, list[dict[str, Any]]] | None] = {}

    async def get_mappings(self, pdb_id: str) -> dict[str, list[dict[str, Any]]] | None:
        """Return ``{uniprot_accession: [segment, ...]}`` for ``pdb_id``.

        Each segment is ``{chain_id, author_start, author_end, unp_start, unp_end}`` in the
        AUTHOR frame. Returns ``None`` when the PDB has no UniProt cross-reference (a named
        degrade for the caller), and ``{}`` only if the record exists but carries no usable
        segment. Cached in-process + on disk (immutable). Raises on a non-404 network
        failure (the caller degrades loud).
        """
        key = pdb_id.lower()
        if key in self._mem:
            return self._mem[key]

        path = _cache.cache_path(_CACHE_SUBDIR, key)
        cached = _cache.read_json(path)  # immutable → no TTL
        if cached is not None:
            parsed = cached if cached else None
            self._mem[key] = parsed
            return parsed

        try:
            body = await self._get_json(f"/mappings/uniprot/{key}")
        except HttpClientError as exc:
            if "404" in str(exc):
                _cache.write_json(path, {})  # record the no-xref result (immutable)
                self._mem[key] = None
                return None
            raise

        parsed = self._parse(body, key)
        _cache.write_json(path, parsed if parsed else {})
        result = parsed if parsed else None
        self._mem[key] = result
        return result

    @staticmethod
    def _parse(body: dict[str, Any], key: str) -> dict[str, list[dict[str, Any]]]:
        uniprot = (body.get(key, {}) or {}).get("UniProt", {}) or {}
        out: dict[str, list[dict[str, Any]]] = {}
        for acc, info in uniprot.items():
            segments: list[dict[str, Any]] = []
            for m in info.get("mappings", []) or []:
                start = m.get("start", {}) or {}
                end = m.get("end", {}) or {}
                a_start = start.get("author_residue_number")
                a_end = end.get("author_residue_number")
                unp_start = m.get("unp_start")
                unp_end = m.get("unp_end")
                if None in (unp_start, unp_end):
                    continue
                # Recover a null author boundary from the segment's constant offset.
                # Within a SIFTS segment the author↔UniProt mapping is co-linear
                # (offset = unp - author is constant; gaps/insertions split into
                # separate segments), so filling a missing edge is EXACT, not a guess.
                # Some entries (e.g. 9NI9 → M4M1I1) carry a null author_residue_number
                # on a segment edge; the old ``None in (a_start, a_end, ...)`` guard
                # discarded the WHOLE mapping, so functional validation falsely reported
                # "no UniProt cross-reference in SIFTS" when one existed — a silent
                # under-reporting failure. Only one anchor is needed to fix the offset;
                # both-null is genuinely unusable for the author-frame bridge.
                if a_start is None and a_end is None:
                    continue
                if a_start is None:
                    a_start = int(unp_start) - (int(unp_end) - int(a_end))
                elif a_end is None:
                    a_end = int(unp_end) - (int(unp_start) - int(a_start))
                segments.append(
                    {
                        "chain_id": m.get("chain_id"),
                        "author_start": int(a_start),
                        "author_end": int(a_end),
                        "unp_start": int(unp_start),
                        "unp_end": int(unp_end),
                    }
                )
            if segments:
                out[acc] = segments
        return out


def chain_segments(mappings: dict[str, list[dict[str, Any]]], chain: str) -> list[dict[str, Any]]:
    """Flatten ``mappings`` to the segments covering ``chain``, each tagged with its
    accession and the constant ``offset = unp_start - author_start`` for that segment.

    Pure function (no I/O) so the +809 numbering bridge is unit-testable offline.
    """
    segs: list[dict[str, Any]] = []
    for acc, seg_list in mappings.items():
        for seg in seg_list:
            if seg["chain_id"] != chain:
                continue
            segs.append(
                {
                    "accession": acc,
                    "author_start": seg["author_start"],
                    "author_end": seg["author_end"],
                    "unp_start": seg["unp_start"],
                    "unp_end": seg["unp_end"],
                    "offset": seg["unp_start"] - seg["author_start"],
                }
            )
    return segs


def bridge_residue(segments: list[dict[str, Any]], author_resi: int) -> tuple[str, int] | None:
    """Map an AUTHOR residue number to ``(accession, unp_pos)`` using the covering segment.

    Returns ``None`` when no segment covers ``author_resi`` (residue outside the modelled,
    UniProt-mapped region). Pure function — the heart of the numbering bridge.
    """
    for seg in segments:
        if seg["author_start"] <= author_resi <= seg["author_end"]:
            return seg["accession"], author_resi + seg["offset"]
    return None


__all__ = ["SiftsClient", "DEFAULT_BASE_URL", "chain_segments", "bridge_residue"]
