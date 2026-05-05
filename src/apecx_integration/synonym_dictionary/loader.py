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
- Ancestor traversal (P3.5): when the dictionary includes a
  ``taxon_hierarchy`` table (built with ``--ncbitaxon-nodes``), queries for
  strain-level NCBITaxon IRIs that miss the fast path are walked upward to
  the nearest ancestor in the dictionary.  This is implemented with a
  SQLite recursive CTE (opens a fresh read-only connection per traversal —
  hierarchy traversal is a miss-path fallback, not the hot path).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.normalization import normalize_surface_form
from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryReader

log = logging.getLogger(__name__)

_NOT_LOADED = object()

_NCBITAXON_OBO_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_"

# SQLite recursive CTE that walks upward from a given taxon to the root (taxid=1).
# Returns all ancestor taxon IDs in order (closest first).
_ANCESTOR_CTE = """
WITH RECURSIVE anc(id, depth) AS (
    SELECT h.parent_taxon_id, 1
    FROM   taxon_hierarchy h
    WHERE  h.child_taxon_id = :taxon_id
    UNION ALL
    SELECT h.parent_taxon_id, anc.depth + 1
    FROM   taxon_hierarchy h
    JOIN   anc ON h.child_taxon_id = anc.id
    WHERE  anc.id != 1
)
SELECT id FROM anc ORDER BY depth
"""


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
        db_path: Path | None = None,
        has_hierarchy: bool = False,
    ) -> None:
        self._inverse = inverse
        self._entries = entries
        self._manifest = manifest
        self._db_path = db_path
        self._has_hierarchy = has_hierarchy

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

    @property
    def has_hierarchy(self) -> bool:
        """True if this index was built with an embedded NCBITaxon hierarchy."""
        return self._has_hierarchy

    def lookup_ancestor(self, iri: str) -> DictionaryEntry | None:
        """Walk the NCBITaxon hierarchy upward to find the nearest ancestor in this dictionary.

        Only meaningful for NCBITaxon IRIs
        (``http://purl.obolibrary.org/obo/NCBITaxon_<id>``).  Returns the
        closest ancestor whose canonical IRI is already in this index, or
        ``None`` when no hierarchy is available or no ancestor matches.

        Opens a fresh read-only SQLite connection per call — this is
        intentionally on the miss/slow path, not the hot path.
        """
        if not self._has_hierarchy or self._db_path is None:
            return None
        if not iri.startswith(_NCBITAXON_OBO_PREFIX):
            return None
        taxon_id_str = iri[len(_NCBITAXON_OBO_PREFIX) :]
        try:
            taxon_id = int(taxon_id_str)
        except ValueError:
            return None

        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            try:
                # Follow merged ID if this taxon was deprecated.
                merge_row = conn.execute(
                    "SELECT new_taxon_id FROM merged_taxons WHERE old_taxon_id = ?",
                    (taxon_id,),
                ).fetchone()
                if merge_row:
                    taxon_id = merge_row[0]

                rows = conn.execute(_ANCESTOR_CTE, {"taxon_id": taxon_id}).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            log.debug("ancestor traversal error for %s: %s", iri, exc)
            return None

        for (ancestor_id,) in rows:
            ancestor_iri = f"{_NCBITAXON_OBO_PREFIX}{ancestor_id}"
            entry = self._entries.get(ancestor_iri)
            if entry is not None:
                return entry
        return None

    def entry_count(self) -> int:
        return len(self._entries)

    def index_entry_count(self) -> int:
        return len(self._inverse)

    def lookup_ambiguous_surface_forms(
        self,
        *,
        surface_form: str | None = None,
        entity_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """Specialized query: ambiguous-synonym catalog.

        Surfaces conflicts captured at build time when two different
        canonical IRIs share the same normalized surface form. The
        ``inverse_index`` keeps last-write-wins for fast runtime lookup,
        but THIS table records the IRIs that lost — so an operator can
        ask "what alternative IRIs does the dictionary know about for
        'vp35'?" and get the full picture.

        Parameters
        ----------
        surface_form:
            Optional filter on the normalized surface form (lowercased,
            stripped).  When ``None``, returns conflicts across all
            surface forms.
        entity_type:
            Optional filter on entity type ("pathogen", "gene", etc.).
        limit:
            Caps the result count so a degenerate dictionary doesn't
            return millions of rows.

        Returns
        -------
        List of dicts, each with keys: ``entity_type``,
        ``surface_form_normalized``, ``winning_canonical_iri``,
        ``alternative_canonical_iri``. Empty list when the dictionary
        has no recorded conflicts (or when filters exclude everything).

        Notes
        -----
        Reads from ``ambiguous_surface_forms`` which was added in the
        2026-05-04 schema (no migration; new dictionaries get it
        automatically, older builds report empty).
        """
        if self._db_path is None:
            return []
        params: list[object] = []
        clauses: list[str] = []
        if surface_form is not None:
            clauses.append("surface_form_normalized = ?")
            params.append(surface_form.strip().lower())
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT entity_type, surface_form_normalized, "
            "winning_canonical_iri, alternative_canonical_iri "
            "FROM ambiguous_surface_forms" + where + " LIMIT ?"
        )
        params.append(int(limit))
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            # Older dictionary built before this table existed.
            return []
        return [
            {
                "entity_type": r[0],
                "surface_form_normalized": r[1],
                "winning_canonical_iri": r[2],
                "alternative_canonical_iri": r[3],
            }
            for r in rows
        ]

    @classmethod
    def load(cls, path: Path | str) -> DictionaryIndex:
        """Load a SQLite dictionary artifact into memory.

        Reads all entries + the full inverse index into Python dicts.
        For VIOLIN+BV-BRC scale (< 10k entries) this completes in
        milliseconds.  If the artifact contains a ``taxon_hierarchy``
        table (built with ``--ncbitaxon-nodes``), ancestor traversal
        is available via :meth:`lookup_ancestor`.
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

        has_hierarchy = reader.has_taxon_hierarchy()
        log.info(
            "loaded dictionary %s: %d entries, %d index rows (version %s, hierarchy=%s)",
            path,
            len(entries),
            len(inverse),
            manifest.dictionary_version,
            has_hierarchy,
        )
        return cls(
            inverse=inverse,
            entries=entries,
            manifest=manifest,
            db_path=path,
            has_hierarchy=has_hierarchy,
        )


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
