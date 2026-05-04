"""SQLite-backed implementation of :class:`DictionaryWriter`.

Persists dictionary entries + inverse index + manifest to a single
SQLite file.  Schema documented in ``synonym_dictionary_contract.md`` §4.

Optional extension: when ``--ncbitaxon-nodes`` is supplied at build time,
the ``taxon_hierarchy`` and ``merged_taxons`` tables are populated from
NCBI's ``nodes.dmp`` / ``merged.dmp``.  These tables power the Stage 2
ancestor traversal in :mod:`apecx_integration.synonym_dictionary.loader`.
"""

from __future__ import annotations

import itertools
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.io import (
    DictionaryReader,
    DictionaryWriter,
)
from apecx_integration.synonym_dictionary.normalization import (
    normalize_surface_form,
)
from apecx_integration.synonym_dictionary.schema import (
    BuildManifest,
    DictionaryEntry,
)

log = logging.getLogger(__name__)


def _batched(it: Iterator, n: int) -> Iterator[list]:
    """Yield successive lists of up to n items from iterator."""
    it = iter(it)
    while batch := list(itertools.islice(it, n)):
        yield batch


_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS entries (
        entity_type           TEXT NOT NULL,
        canonical_iri         TEXT NOT NULL,
        canonical_label       TEXT NOT NULL,
        ontology              TEXT NOT NULL,
        ontology_version      TEXT NOT NULL,
        confidence            REAL NOT NULL,
        resolved_at           TEXT NOT NULL,
        source_records_json   TEXT NOT NULL,
        synonyms_json         TEXT NOT NULL,
        PRIMARY KEY (entity_type, canonical_iri)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS inverse_index (
        entity_type             TEXT NOT NULL,
        surface_form_normalized TEXT NOT NULL,
        canonical_iri           TEXT NOT NULL,
        PRIMARY KEY (entity_type, surface_form_normalized)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS inverse_index_by_surface
        ON inverse_index (surface_form_normalized);
    """,
    """
    CREATE TABLE IF NOT EXISTS manifest (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
)


_MANIFEST_ROW_KEY = "manifest_json"

_HIERARCHY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS taxon_hierarchy (
        child_taxon_id  INTEGER NOT NULL,
        parent_taxon_id INTEGER NOT NULL,
        PRIMARY KEY (child_taxon_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_taxon_hierarchy_child
        ON taxon_hierarchy (child_taxon_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS merged_taxons (
        old_taxon_id INTEGER NOT NULL PRIMARY KEY,
        new_taxon_id INTEGER NOT NULL
    );
    """,
)

_HIERARCHY_BATCH = 50_000


class SQLiteDictionaryWriter(DictionaryWriter):
    """Writer that persists to a SQLite file.

    Use as a context manager so the connection is closed cleanly:

    .. code-block:: python

        with SQLiteDictionaryWriter(Path("build/dictionary.sqlite")) as w:
            for entry in entries:
                w.write_entry(entry)
            w.write_manifest(manifest)
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        for ddl in _SCHEMA_DDL:
            self._conn.execute(ddl)
        self._conn.commit()

    def write_entry(self, entry: DictionaryEntry) -> None:
        # Idempotent: replace any prior entry for this (entity_type, canonical_iri).
        self._conn.execute(
            """
            INSERT INTO entries (
                entity_type, canonical_iri, canonical_label, ontology,
                ontology_version, confidence, resolved_at,
                source_records_json, synonyms_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, canonical_iri) DO UPDATE SET
                canonical_label     = excluded.canonical_label,
                ontology            = excluded.ontology,
                ontology_version    = excluded.ontology_version,
                confidence          = excluded.confidence,
                resolved_at         = excluded.resolved_at,
                source_records_json = excluded.source_records_json,
                synonyms_json       = excluded.synonyms_json
            """,
            (
                entry.entity_type.value,
                entry.canonical_iri,
                entry.canonical_label,
                entry.ontology.value,
                entry.ontology_version,
                entry.confidence,
                entry.resolved_at.isoformat(),
                json.dumps(list(entry.source_records)),
                json.dumps(list(entry.synonyms)),
            ),
        )
        # Inverse index: every synonym + the canonical label all map back to
        # this canonical IRI.  Normalize at write-time so the runtime read
        # path is a direct equality lookup.
        for surface in (entry.canonical_label, *entry.synonyms):
            normalized = normalize_surface_form(surface)
            if not normalized:
                continue
            self._conn.execute(
                """
                INSERT OR REPLACE INTO inverse_index (
                    entity_type, surface_form_normalized, canonical_iri
                )
                VALUES (?, ?, ?)
                """,
                (entry.entity_type.value, normalized, entry.canonical_iri),
            )

    def write_manifest(self, manifest: BuildManifest) -> None:
        # Pydantic's model_dump_json serialises datetimes + Enums correctly.
        payload = manifest.model_dump_json()
        self._conn.execute(
            """
            INSERT INTO manifest (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_MANIFEST_ROW_KEY, payload),
        )

    def write_taxon_hierarchy(self, nodes_iter: Iterator[tuple[int, int]]) -> int:
        """Persist the full NCBITaxon parent-child mapping from ``nodes.dmp``.

        ``nodes_iter`` yields ``(child_taxon_id, parent_taxon_id)`` pairs.
        Returns the number of rows written.  Idempotent: duplicate
        child_taxon_id rows are silently ignored (INSERT OR IGNORE).
        """
        for ddl in _HIERARCHY_DDL:
            self._conn.execute(ddl)
        count = 0
        for batch in _batched(nodes_iter, _HIERARCHY_BATCH):
            self._conn.executemany(
                "INSERT OR IGNORE INTO taxon_hierarchy "
                "(child_taxon_id, parent_taxon_id) VALUES (?, ?)",
                batch,
            )
            count += len(batch)
            self._conn.commit()
        log.info("taxon_hierarchy: wrote %d rows", count)
        return count

    def write_merged_taxons(self, merged_iter: Iterator[tuple[int, int]]) -> int:
        """Persist the NCBITaxon merged-ID table from ``merged.dmp``.

        ``merged_iter`` yields ``(old_taxon_id, new_taxon_id)`` pairs.
        Returns the number of rows written.
        """
        for ddl in _HIERARCHY_DDL:
            self._conn.execute(ddl)
        count = 0
        for batch in _batched(merged_iter, _HIERARCHY_BATCH):
            self._conn.executemany(
                "INSERT OR REPLACE INTO merged_taxons "
                "(old_taxon_id, new_taxon_id) VALUES (?, ?)",
                batch,
            )
            count += len(batch)
        self._conn.commit()
        log.info("merged_taxons: wrote %d rows", count)
        return count

    def has_taxon_hierarchy(self) -> bool:
        """Return True if the hierarchy table was written and is non-empty."""
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM taxon_hierarchy LIMIT 1").fetchone()
            return bool(row and row[0] > 0)
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


class SQLiteDictionaryReader(DictionaryReader):
    """Reader that loads dictionary entries from a SQLite file built by
    :class:`SQLiteDictionaryWriter`."""

    # Major schema versions this reader supports.  Bump in lockstep with
    # the writer when introducing breaking changes.
    SUPPORTED_SCHEMA_MAJOR: tuple[int, ...] = (1,)

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"dictionary artifact not found: {self._path}")
        self._conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        # Validate schema version eagerly so callers see incompatible-version
        # errors at construction time, not at first lookup.
        manifest = self.read_manifest()
        major = int(manifest.schema_version.split(".", 1)[0])
        if major not in self.SUPPORTED_SCHEMA_MAJOR:
            raise ValueError(
                f"dictionary schema major v{major} not supported by this "
                f"reader (supported: {self.SUPPORTED_SCHEMA_MAJOR})"
            )

    def read_manifest(self) -> BuildManifest:
        row = self._conn.execute(
            "SELECT value FROM manifest WHERE key = ?",
            (_MANIFEST_ROW_KEY,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"dictionary at {self._path} has no manifest row — " "build was incomplete"
            )
        return BuildManifest.model_validate_json(row["value"])

    def lookup_by_surface_form(
        self, entity_type: EntityType, surface_form: str
    ) -> DictionaryEntry | None:
        normalized = normalize_surface_form(surface_form)
        if not normalized:
            return None
        row = self._conn.execute(
            """
            SELECT canonical_iri FROM inverse_index
            WHERE entity_type = ? AND surface_form_normalized = ?
            """,
            (entity_type.value, normalized),
        ).fetchone()
        if row is None:
            return None
        return self.lookup_by_iri(row["canonical_iri"])

    def lookup_by_iri(self, canonical_iri: str) -> DictionaryEntry | None:
        row = self._conn.execute(
            """
            SELECT entity_type, canonical_iri, canonical_label, ontology,
                   ontology_version, confidence, resolved_at,
                   source_records_json, synonyms_json
            FROM entries
            WHERE canonical_iri = ?
            """,
            (canonical_iri,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def has_taxon_hierarchy(self) -> bool:
        """Return True if the SQLite file contains a non-empty taxon_hierarchy table."""
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM taxon_hierarchy LIMIT 1").fetchone()
            return bool(row and row[0] > 0)
        except sqlite3.OperationalError:
            return False

    def all_entries(self) -> Iterator[DictionaryEntry]:
        for row in self._conn.execute(
            """
            SELECT entity_type, canonical_iri, canonical_label, ontology,
                   ontology_version, confidence, resolved_at,
                   source_records_json, synonyms_json
            FROM entries
            """
        ):
            yield self._row_to_entry(row)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> DictionaryEntry:
        from datetime import datetime  # local import keeps top of file lean

        from apecx_integration.synonym_dictionary.enums import OntologyName

        return DictionaryEntry(
            entity_type=EntityType(row["entity_type"]),
            canonical_iri=row["canonical_iri"],
            canonical_label=row["canonical_label"],
            ontology=OntologyName(row["ontology"]),
            ontology_version=row["ontology_version"],
            confidence=row["confidence"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]),
            source_records=tuple(json.loads(row["source_records_json"])),
            synonyms=tuple(json.loads(row["synonyms_json"])),
        )
