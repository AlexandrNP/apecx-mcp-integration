"""Resolve an organism name to its NCBITaxon PURL IRI.

Upstream of the identifier-filter step: turns a free-text organism term
("CHIKV", "Chikungunya virus") into the canonical NCBITaxon IRI the filter
keys on. Degrades LOUD on a miss (returns ``None``, never raises).

Reuse, not re-implementation:
- normalization goes through the SAME
  :func:`apecx_integration.synonym_dictionary.normalization.normalize_surface_form`
  used at build time, so runtime keys agree with the index.
- the DB read mirrors ``gazetteer.build_from_dictionary`` — a read-only
  ``inverse_index`` query — rather than the ``lookup_entity`` singleton path,
  which loads the full dictionary and mutates process-wide state (awkward to
  point at a fixture in a unit test).

``sqlite3`` is imported lazily inside the function so this module imports
cleanly on a bare install.
"""

from __future__ import annotations

import contextlib
import os

# The organism identifier space this resolver returns. Filtering on it means a
# surface form that also maps to a non-organism IRI still yields the NCBITaxon
# one (or ``None`` if there is no organism sense).
_NCBITAXON_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_"


def resolve_organism_to_iri(term: str, db_path: str | os.PathLike | None = None) -> str | None:
    """Return the NCBITaxon PURL IRI for ``term``, or ``None`` on a miss.

    ``term`` is normalized with the shared build-time normalizer, then looked
    up in the dictionary's ``inverse_index`` for an NCBITaxon canonical IRI.
    ``db_path`` defaults to the canonical dictionary location
    (:func:`default_dictionary_path`) when omitted.
    """
    import sqlite3

    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    normalized = normalize_surface_form(term)
    if not normalized:
        return None

    if db_path is None:
        from apecx_integration.synonym_dictionary.loader import default_dictionary_path

        db_path = default_dictionary_path()

    sql = (
        "SELECT canonical_iri FROM inverse_index "
        "WHERE surface_form_normalized = ? AND canonical_iri LIKE ? "
        "LIMIT 1"
    )
    uri = f"file:{db_path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        row = conn.execute(sql, (normalized, f"{_NCBITAXON_PREFIX}%")).fetchone()
    return row[0] if row else None
