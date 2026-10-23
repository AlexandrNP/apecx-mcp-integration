"""Resolve an organism name to its NCBITaxon PURL IRI.

Upstream of the identifier-filter step: turns a free-text organism term
("CHIKV", "Chikungunya virus") into the canonical NCBITaxon IRI the filter
keys on. Degrades LOUD on a miss (returns ``None``, never raises).

Reuse, not re-implementation:
- normalization goes through the SAME
  :func:`apecx_integration.synonym_dictionary.normalization.normalize_surface_form`
  used at build time, so runtime keys agree with the index.
- the DB read mirrors ``gazetteer.build_from_dictionary`` — read-only queries
  against the dictionary file — rather than the ``lookup_entity`` singleton
  path, which loads the full dictionary and mutates process-wide state.

Ambiguity is a FIRST-CLASS outcome here, not a silent pick. ``inverse_index``
is a last-write-wins table (``sqlite_writer`` upserts with INSERT OR REPLACE
and shunts the losers into ``ambiguous_surface_forms``), so a bare
``... LIMIT 1`` read returns whichever row was written last — unrelated to
correctness. "RSV", for instance, collides across six taxa including *Rous
sarcoma virus* and a rice tenuivirus. :func:`resolve_organism` therefore
consults ``ambiguous_surface_forms`` and reports the candidates instead of
guessing; :func:`resolve_organism_to_iri` keeps its old signature but now
fails CLOSED (``None``) on an ambiguous term.

Why a targeted query rather than
:func:`apecx_integration.synonym_dictionary.lookup.detect_ambiguity`: that
path loads the whole dictionary into the process singleton — measured at
~17s and >2 GB RSS on a cold process — for an answer this module gets from
one indexed lookup in well under a millisecond. The targeted read also keeps
``db_path`` meaningful, so unit tests can still point at a small fixture.

``sqlite3`` is imported lazily inside the functions so this module imports
cleanly on a bare install.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Literal, NamedTuple

# The organism identifier space this resolver returns. Filtering on it means a
# surface form that also maps to a non-organism IRI still yields the NCBITaxon
# one (or ``None`` if there is no organism sense).
_NCBITAXON_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_"

ResolutionOutcome = Literal["resolved", "ambiguous", "miss"]


class OrganismResolution(NamedTuple):
    """Three-state outcome of resolving an organism surface form.

    ``iri`` is populated ONLY for ``status == "resolved"``. It is deliberately
    ``None`` on ``"ambiguous"`` so a caller that ignores ``status`` and uses
    the IRI anyway fails loudly rather than filtering on a coin-flip taxon.
    """

    iri: str | None
    candidates: list[dict[str, Any]]
    status: ResolutionOutcome


def _ambiguous_candidates(conn: Any, normalized: str) -> list[dict[str, Any]]:
    """Return the candidate taxa for ``normalized``, or ``[]`` if unambiguous.

    Reads ``ambiguous_surface_forms`` — the build-time record of surfaces whose
    ``inverse_index`` row had to displace another. Both sides of each conflict
    row are collected, so the winner is included alongside its alternatives.

    The emitted dicts carry the SAME four keys as
    :func:`synonym_dictionary.lookup.detect_ambiguity` candidates
    (``canonical_iri`` / ``canonical_label`` / ``canonical_ontology`` /
    ``confidence``) so downstream envelope builders consume either unchanged.

    Dictionaries built before the ambiguity pass have no such table; that is a
    degrade (treat as unambiguous), not an error — mirroring
    ``DictionaryIndex.lookup_ambiguous_surface_forms``.
    """
    import sqlite3

    try:
        rows = conn.execute(
            "SELECT winning_canonical_iri, alternative_canonical_iri "
            "FROM ambiguous_surface_forms WHERE surface_form_normalized = ?",
            (normalized,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    iris: list[str] = []
    for winning, alternative in rows:
        for iri in (winning, alternative):
            if iri and iri.startswith(_NCBITAXON_PREFIX) and iri not in iris:
                iris.append(iri)
    if len(iris) < 2:
        return []

    labels: dict[str, str] = {}
    with contextlib.suppress(sqlite3.OperationalError):
        placeholders = ",".join("?" * len(iris))
        labels = dict(
            conn.execute(
                f"SELECT canonical_iri, canonical_label FROM entries "  # noqa: S608 - fixed arity
                f"WHERE canonical_iri IN ({placeholders})",
                iris,
            ).fetchall()
        )

    return [
        {
            "canonical_iri": iri,
            "canonical_label": labels.get(iri),
            "canonical_ontology": "ncbitaxon",
            "confidence": 1.0 if iri in labels else 0.0,
        }
        for iri in iris
    ]


def resolve_organism(term: str, db_path: str | os.PathLike | None = None) -> OrganismResolution:
    """Resolve ``term`` to an NCBITaxon IRI, reporting ambiguity instead of guessing.

    Returns an :class:`OrganismResolution` whose ``status`` is one of:

    - ``"resolved"`` — exactly one organism sense; ``iri`` is set.
    - ``"ambiguous"`` — the surface form collides across ≥2 taxa. ``iri`` is
      ``None`` and ``candidates`` holds the competing entries. The caller MUST
      surface them and stop (the structural HITL gate ``harmonized_search``
      and the database tools already implement).
    - ``"miss"`` — no organism sense; ``iri`` is ``None``.

    An NCBITaxon IRI passed as ``term`` short-circuits to ``"resolved"``: the
    caller has already disambiguated, which is exactly how a re-call after an
    ambiguity pause arrives. Without this, answering the pause would look like
    a miss.

    Never raises on a miss. ``db_path`` defaults to the canonical dictionary
    location (:func:`default_dictionary_path`) when omitted.
    """
    import sqlite3

    from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

    if isinstance(term, str) and term.startswith(_NCBITAXON_PREFIX):
        return OrganismResolution(term.strip(), [], "resolved")

    normalized = normalize_surface_form(term)
    if not normalized:
        return OrganismResolution(None, [], "miss")

    if db_path is None:
        from apecx_integration.synonym_dictionary.loader import default_dictionary_path

        db_path = default_dictionary_path()

    uri = f"file:{db_path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        candidates = _ambiguous_candidates(conn, normalized)
        if candidates:
            return OrganismResolution(None, candidates, "ambiguous")

        row = conn.execute(
            "SELECT canonical_iri FROM inverse_index "
            "WHERE surface_form_normalized = ? AND canonical_iri LIKE ? "
            "LIMIT 1",
            (normalized, f"{_NCBITAXON_PREFIX}%"),
        ).fetchone()

    if row:
        return OrganismResolution(row[0], [], "resolved")
    return OrganismResolution(None, [], "miss")


def resolve_organism_to_iri(term: str, db_path: str | os.PathLike | None = None) -> str | None:
    """Return the NCBITaxon PURL IRI for ``term``, or ``None``.

    Back-compatible wrapper over :func:`resolve_organism` for callers that
    cannot act on a candidate list. ``None`` now covers BOTH a genuine miss and
    an ambiguous term — failing closed, because the alternative is filtering a
    literature corpus on an arbitrarily-chosen taxon. Callers that can pause
    for the user should use :func:`resolve_organism` and show the candidates.
    """
    return resolve_organism(term, db_path).iri
