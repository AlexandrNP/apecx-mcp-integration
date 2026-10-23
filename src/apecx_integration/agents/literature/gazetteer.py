"""Deterministic surface-form → NCBITaxon IRI tagger (skeleton).

A dependency-free longest-match matcher over word-window n-grams (1-4 words):
no pyahocorasick, no new dependency. The normalizer is lazy-imported so this
module imports cleanly on a bare install; if the synonym-dictionary normalizer
is unavailable it degrades to ``s.lower().strip()``.

The precision guards (min length, non-alphabetic, generic-word blocklist) are
the load-bearing part — they stop generic tokens ("virus", "gene") from being
stamped as a taxon.
"""

from __future__ import annotations

import contextlib
import re
from typing import NamedTuple

# Generic words that must never be stamped as a taxon even if a caller injects
# them into the map. Applied at match time, so the map contents can't defeat it.
_BLOCKLIST = frozenset({"virus", "viruses", "vira", "the", "and", "protein", "gene"})
_MIN_SURFACE_LEN = 3
_MAX_NGRAM_WORDS = 4
_WORD = re.compile(r"\S+")


class Tag(NamedTuple):
    """A single taxon match: the raw ``surface`` text at ``[start, end)`` and
    the ``iri`` it resolved to."""

    surface: str
    iri: str
    start: int
    end: int


def _normalize(s: str) -> str:
    """Canonicalize a surface form, reusing the dictionary normalizer when
    present so build-time and runtime keys agree; else lowercase+strip."""
    try:
        from apecx_integration.synonym_dictionary.normalization import (
            normalize_surface_form,
        )

        return normalize_surface_form(s)
    except ImportError:
        return s.lower().strip()


def _is_taggable(normalized: str) -> bool:
    """Precision guard: reject too-short, non-alphabetic, or blocklisted forms."""
    if len(normalized) < _MIN_SURFACE_LEN:
        return False
    if normalized in _BLOCKLIST:
        return False
    return any(c.isalpha() for c in normalized)


class Gazetteer:
    """In-memory case-insensitive longest-match taxon tagger."""

    def __init__(self, term_iri_map: dict[str, str]) -> None:
        # Normalize keys once at construction so tag() compares like-for-like.
        self._map = {_normalize(surface): iri for surface, iri in term_iri_map.items()}

    def tag(self, text: str) -> list[Tag]:
        """Return non-overlapping longest-match tags, left to right.

        Deterministic: the same text yields identical tags every call.
        """
        tokens = [(m.start(), m.end()) for m in _WORD.finditer(text)]
        tags: list[Tag] = []
        i = 0
        n = len(tokens)
        while i < n:
            span = min(_MAX_NGRAM_WORDS, n - i)
            while span > 0:
                start = tokens[i][0]
                end = tokens[i + span - 1][1]
                surface = text[start:end]
                key = _normalize(surface)
                if key in self._map and _is_taggable(key):
                    tags.append(Tag(surface, self._map[key], start, end))
                    i += span
                    break
                span -= 1
            else:
                i += 1
        return tags


def build_gazetteer(term_iri_map: dict[str, str]) -> Gazetteer:
    """Build a gazetteer from an INJECTED {surface_form: NCBITaxon_IRI} map."""
    return Gazetteer(term_iri_map)


def _ambiguous_surfaces(conn: object) -> set[str]:
    """Surface forms known to collide across ≥2 taxa, per the dictionary build.

    ``inverse_index`` keeps one row per surface form (last write wins) and
    shunts the displaced conflicts into ``ambiguous_surface_forms``. Stamping
    an abstract from the winner alone attributes e.g. every "RSV" mention to
    whichever of six taxa happened to be written last — and because the query
    side reads the same table, the wrong stamp and the wrong query agree and
    the error is invisible downstream. Excluding these surfaces makes the
    tagger abstain where it cannot be sure.

    Older dictionaries predate the table; absence means "no known conflicts",
    not a failure.
    """
    import sqlite3

    try:
        return {
            row[0]
            for row in conn.execute(  # type: ignore[attr-defined]
                "SELECT DISTINCT surface_form_normalized FROM ambiguous_surface_forms"
            )
        }
    except sqlite3.OperationalError:
        return set()


def build_from_dictionary(
    db_path: str,
    *,
    entity_type: str = "pathogen",
    limit: int | None = None,
    exclude_ambiguous: bool = True,
) -> Gazetteer:
    """Build a gazetteer by streaming the ``inverse_index`` of dictionary.sqlite.

    Reads ``(surface_form_normalized, canonical_iri)`` rows for ``entity_type``,
    keeping only surfaces that pass the same precision guard as the tag path
    (:func:`_is_taggable`), and returns a gazetteer over the resulting map.
    ``limit`` bounds the number of rows read (SQL ``LIMIT``) for cheap builds.

    ``exclude_ambiguous`` (default on) additionally drops surfaces listed in
    ``ambiguous_surface_forms`` — see :func:`_ambiguous_surfaces`. On the
    shipped dictionary this removes 588 of ~1.34M keys (0.04%), among them
    "rna", "dna" and "rsv". Filtering happens in Python rather than in SQL so
    that the query — and therefore ``limit``'s row-order semantics — is
    unchanged, and so a dictionary without the table degrades silently.
    """
    import sqlite3

    sql = "SELECT surface_form_normalized, canonical_iri FROM inverse_index WHERE entity_type = ?"
    params: tuple[object, ...] = (entity_type,)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)

    uri = f"file:{db_path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        ambiguous = _ambiguous_surfaces(conn) if exclude_ambiguous else set()
        term_iri_map = {
            surface: iri
            for surface, iri in conn.execute(sql, params)
            if _is_taggable(surface) and surface not in ambiguous
        }
    return build_gazetteer(term_iri_map)
