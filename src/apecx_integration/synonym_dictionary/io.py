"""Abstract IO contracts for the synonym dictionary.

Concrete implementations land in Phase 2 (SQLite-backed writer in
``sqlite_writer.py``, in-memory reader in ``loader.py``).  These ABCs
exist now so downstream code (Stage 2, MCP tools, tests) can be written
against the contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.schema import (
    BuildManifest,
    DictionaryEntry,
)


class DictionaryWriter(ABC):
    """Build-time interface for emitting a dictionary artifact.

    Implementations are expected to be context managers (so the writer
    can flush + close on exit), but the ABC itself only requires
    explicit ``close()`` for portability.
    """

    @abstractmethod
    def write_entry(self, entry: DictionaryEntry) -> None:
        """Persist one dictionary entry.  Idempotent on
        ``(entity_type, canonical_iri)`` — re-writing the same key
        replaces any prior entry."""

    @abstractmethod
    def write_manifest(self, manifest: BuildManifest) -> None:
        """Persist the build manifest.  Should be called exactly once
        per dictionary artifact, after all entries are written."""

    @abstractmethod
    def close(self) -> None:
        """Flush + finalize the artifact.  After calling this, the
        writer is no longer usable."""

    def __enter__(self) -> DictionaryWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class DictionaryReader(ABC):
    """Runtime interface for querying a built dictionary artifact.

    Stage 2's lookup API consumes one of these.  Concrete implementations
    are expected to load the artifact eagerly at construction (so reads
    are fast / lock-free).
    """

    @abstractmethod
    def read_manifest(self) -> BuildManifest:
        """Return the build manifest.  Raises if the artifact's
        ``schema_version`` is incompatible with this reader."""

    @abstractmethod
    def lookup_by_surface_form(
        self, entity_type: EntityType, surface_form: str
    ) -> DictionaryEntry | None:
        """Resolve a free-text user term to its canonical entry.

        ``surface_form`` is matched against the entry's
        ``canonical_label`` and every value in ``synonyms``, after the
        same normalization Stage 1 applied at build time (case-fold,
        Unicode NFKC, whitespace collapse).

        Returns ``None`` when no entry matches — Stage 2's caller is
        responsible for routing to the slow path in that case.
        """

    @abstractmethod
    def lookup_by_iri(self, canonical_iri: str) -> DictionaryEntry | None:
        """Direct lookup by canonical IRI.  Used for the cross-database
        join path: given an IRI from one enriched table, retrieve the
        synonyms / canonical_label without re-resolving."""

    @abstractmethod
    def all_entries(self) -> Iterator[DictionaryEntry]:
        """Iterate every entry in the dictionary.  Used for build
        verification, audit dumps, and re-resolution flows."""

    def __enter__(self) -> DictionaryReader:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Default: no-op.  Concrete readers may override for resource cleanup.
        return None
