"""Stage 2 runtime dictionary loader.

Loads the SQLite dictionary artifact produced by Stage 1's
:func:`apecx_integration.synonym_dictionary.build.build_dictionary` into
an in-memory structure that supports O(1) lookup per user query.

Design decisions:
- Dictionary loaded ONCE at first use (lazy) and held for the process
  lifetime.  No per-request cost on the hot path.
- Storage: the full inverse index (surface_form_normalized -> canonical_iri)
  fits in a plain Python dict for VIOLIN+BV-BRC scale.  At production
  scale (millions of entries), a trie would be appropriate; not built here.
- Thread safety: load is protected by a threading.Lock.  Concurrent reads
  after load are lock-free (plain dict reads in CPython hold the GIL, so
  they're safe without explicit locking).
- No on-disk caching beyond what SQLite already provides; the SQLite file
  is the cache.

Open decision (P3.5 — requires user direction):
  Taxonomic-graph traversal is NOT implemented here.  A query for
  "Eastern equine encephalitis virus" (species-level IRI) would NOT match
  BV-BRC rows whose genome_id maps to a strain-level descendant taxon.
  See ontology_integration_initial_analysis.md §4.9(1) for the design
  options (bundled hierarchy snapshot vs. live OLS at runtime).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.normalization import normalize_surface_form
from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

log = logging.getLogger(__name__)

_NOT_LOADED = object()


class DictionaryIndex:
    """In-memory index loaded from a SQLite dictionary artifact.

    Use :func:`load` to construct an instance from a path.  The ``lookup``
    method is the Stage 2 hot path.
    """

    def __init__(
        self,
        *,
        inverse: dict[tuple[str, str], str],
        entries: dict[str, DictionaryEntry],
        manifest: BuildManifest,
    ) -> None:
        self._inverse = inverse
        self._entries = entries
        self._manifest = manifest

    @property
    def manifest(self) -> BuildManifest:
        return self._manifest

    def lookup(self, entity_type: EntityType, surface_form: str) -> DictionaryEntry | None:
        """Fast-path lookup: normalized surface form -> canonical entry.

        Returns ``None`` on miss (caller should route to slow path).
        """
        normalized = normalize_surface_form(surface_form)
        if not normalized:
            return None
        canonical_iri = self._inverse.get((entity_type.value, normalized))
        if canonical_iri is None:
            return None
        return self._entries.get(canonical_iri)

    def lookup_any_type(self, surface_form: str) -> list[DictionaryEntry]:
        """Search across all entity types.

        Used when the caller doesn't know the entity type in advance (e.g.
        a free-text query that could be a pathogen OR a vaccine).  Returns
        all matches, ordered by confidence descending.
        """
        normalized = normalize_surface_form(surface_form)
        if not normalized:
            return []
        results: list[DictionaryEntry] = []
        seen_iris: set[str] = set()
        for (_, norm_form), iri in self._inverse.items():
            if norm_form == normalized and iri not in seen_iris:
                entry = self._entries.get(iri)
                if entry is not None:
                    results.append(entry)
                    seen_iris.add(iri)
        results.sort(key=lambda e: e.confidence, reverse=True)
        return results

    def lookup_by_iri(self, canonical_iri: str) -> DictionaryEntry | None:
        """Reverse lookup: canonical IRI -> dictionary entry.

        Used when the caller already has an IRI (e.g. from a prior
        resolution step or from a cross-database join) and wants to
        retrieve the canonical label and synonym set.

        Returns ``None`` when the IRI is not present in this index build.
        """
        return self._entries.get(canonical_iri)

    def entry_count(self) -> int:
        return len(self._entries)

    def index_entry_count(self) -> int:
        return len(self._inverse)

    @classmethod
    def load(cls, path: Path | str) -> DictionaryIndex:
        """Load a SQLite dictionary artifact into memory.

        Reads all entries + the full inverse index into Python dicts.
        For VIOLIN+BV-BRC scale (< 10k entries) this completes in
        milliseconds.
        """
        path = Path(path)
        reader = SQLiteDictionaryReader(path)
        manifest = reader.read_manifest()

        inverse: dict[tuple[str, str], str] = {}
        entries: dict[str, DictionaryEntry] = {}

        for entry in reader.all_entries():
            entries[entry.canonical_iri] = entry
            for surface in (entry.canonical_label, *entry.synonyms):
                normalized = normalize_surface_form(surface)
                if normalized:
                    key = (entry.entity_type.value, normalized)
                    if key not in inverse:
                        inverse[key] = entry.canonical_iri

        log.info(
            "loaded dictionary %s: %d entries, %d index rows (version %s)",
            path,
            len(entries),
            len(inverse),
            manifest.dictionary_version,
        )
        return cls(inverse=inverse, entries=entries, manifest=manifest)


class _ProcessSingleton:
    """Lazy process-singleton holder for the DictionaryIndex.

    The MCP server loads the dictionary once at first use, not at
    startup — startup is not blocked if no dictionary artifact exists
    (the Stage 2 fast path degrades to slow-path-only gracefully).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._index: DictionaryIndex | object = _NOT_LOADED
        self._path: Path | None = None
        self._error: str | None = None

    def configure(self, path: Path | str) -> None:
        """Set the dictionary artifact path.  Call before ``get``."""
        with self._lock:
            new_path = Path(path)
            if new_path != self._path:
                self._path = new_path
                self._index = _NOT_LOADED
                self._error = None

    def get(self) -> tuple[DictionaryIndex | None, str | None]:
        """Return (index, None) on success or (None, error_message) on failure.

        Thread-safe: concurrent callers block until the first load completes.
        """
        if self._index is not _NOT_LOADED:
            if isinstance(self._index, DictionaryIndex):
                return self._index, None
            return None, self._error

        with self._lock:
            if self._index is _NOT_LOADED:
                if self._path is None:
                    self._error = (
                        "APECX_SYNONYM_DICT_PATH not set; Stage 2 fast path "
                        "is disabled. Run apecx-build-dictionary to produce "
                        "a dictionary artifact."
                    )
                    return None, self._error
                try:
                    self._index = DictionaryIndex.load(self._path)
                except Exception as exc:
                    self._error = f"Failed to load dictionary from {self._path}: {exc}"
                    log.warning(self._error)
                    return None, self._error
            if isinstance(self._index, DictionaryIndex):
                return self._index, None
            return None, self._error


_singleton = _ProcessSingleton()


def configure_dictionary_path(path: Path | str) -> None:
    """Set the process-wide dictionary artifact path.

    Typically called from the MCP server startup with the value of
    ``APECX_SYNONYM_DICT_PATH``.  Idempotent: if the path changes,
    the next ``get_dictionary_index`` call re-loads.
    """
    _singleton.configure(path)


def get_dictionary_index() -> tuple[DictionaryIndex | None, str | None]:
    """Return the process-wide DictionaryIndex or an error message.

    Returns ``(index, None)`` when the dictionary is loaded, or
    ``(None, error_string)`` when unavailable.  The caller should
    gracefully fall back to the slow path on ``None``.
    """
    return _singleton.get()
