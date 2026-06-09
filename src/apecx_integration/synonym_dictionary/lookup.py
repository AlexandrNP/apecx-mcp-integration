"""Stage 2 runtime lookup API.

Exposes a single entry point -- :func:`lookup_entity` -- that:

1. **Fast path**: normalizes the user term and looks it up in the
   in-memory :class:`~apecx_integration.synonym_dictionary.loader.DictionaryIndex`.
   O(1) hash lookup; returns in microseconds.

2. **Ancestor path**: on a fast-path IRI miss, walks the NCBITaxon
   hierarchy (``taxon_hierarchy`` table) upward to the nearest ancestor
   whose IRI IS in the dictionary.  Only active when the dictionary was
   built with ``--ncbitaxon-nodes``; degrades gracefully to slow path
   when the table is absent.

3. **Slow path**: on a fast-path miss, falls back to the existing
   substring-matching logic in
   :mod:`apecx_integration.mcp_surface.data.database` (which benefits
   from the Phase 0 pre-filter fix in apecx-db-integration).  The slow
   path is inherently approximate; a HITL gate is upstream of this module.

Visibility requirement (analysis doc §0.1, §6.2.1):
  The lookup result ALWAYS includes which path was taken and at what
  confidence.  Stage 2 MUST NOT silently route "EEEV" to an IRI without
  surfacing that decision.  The :class:`LookupResult` type makes the
  routing decision explicit and machine-readable.

What this module does NOT do:
- Provisional-synonym lookup (Phase 4 — deferred per analysis doc §6.1).
- Per-user or per-session caching (unnecessary at current scale).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from apecx_integration.synonym_dictionary.enums import EntityType, ResolutionStatus
from apecx_integration.synonym_dictionary.loader import get_dictionary_index
from apecx_integration.synonym_dictionary.schema import DictionaryEntry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LookupResult:
    """The outcome of a single Stage 2 entity lookup.

    ``path`` values:
    - ``"fast"``     — exact dictionary hit (canonical IRI or synonym).
    - ``"ancestor"`` — IRI miss but a taxonomic ancestor was found in the
                       dictionary via the NCBITaxon hierarchy table.
                       Confidence is the ancestor entry's confidence × 0.9.
    - ``"slow"``     — fell through to database substring matcher.
    - ``"miss"``     — no match on any path.
    """

    surface_form: str
    path: Literal["fast", "ancestor", "slow", "miss"]
    canonical_iri: str | None
    canonical_label: str | None
    canonical_ontology: str | None
    confidence: float
    resolution_status: ResolutionStatus
    synonyms: tuple[str, ...] = field(default_factory=tuple)
    evidence: str = ""


def fast_miss(surface_form: str, *, reason: str = "") -> LookupResult:
    """Return a LookupResult representing a complete miss on all paths."""
    return LookupResult(
        surface_form=surface_form,
        path="miss",
        canonical_iri=None,
        canonical_label=None,
        canonical_ontology=None,
        confidence=0.0,
        resolution_status=ResolutionStatus.UNRESOLVED,
        evidence=reason,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def lookup_entity(
    surface_form: str,
    *,
    entity_type: EntityType | None = None,
) -> LookupResult:
    """Look up a user-supplied term against the synonym dictionary.

    Parameters
    ----------
    surface_form:
        The user-typed term ("EEEV", "Eastern equine encephalitis virus",
        "Chikungunya", etc.).
    entity_type:
        Optional hint.  When ``None``, search all entity types and return
        the highest-confidence match.  When supplied, restrict to that type.

    Returns
    -------
    A :class:`LookupResult` with ``path`` set to:

    - ``"fast"``     — exact dictionary hit.
    - ``"ancestor"`` — strain-level IRI matched via NCBITaxon ancestor chain.
    - ``"slow"``     — substring match in the database store (degraded path).
    - ``"miss"``     — no match on any path.

    The visibility guarantee: the caller sees WHICH path was taken and at
    what confidence.  Do not suppress this into a naked "entity not found".
    """
    if not surface_form or not surface_form.strip():
        return fast_miss(surface_form, reason="empty input")

    index, load_error = get_dictionary_index()

    # Fast path — IRI shortcut: if the caller already has a canonical IRI,
    # skip surface-form normalization and look it up directly.
    if index is not None and (
        surface_form.startswith("http://") or surface_form.startswith("https://")
    ):
        entry = index.lookup_by_iri(surface_form)
        if entry is not None:
            return _entry_to_result(surface_form, entry, path="fast")

        # IRI miss — try taxonomic ancestor traversal (NCBITaxon hierarchy).
        ancestor = index.lookup_ancestor(surface_form)
        if ancestor is not None:
            return _ancestor_to_result(surface_form, ancestor)

    # Fast path — surface form lookup
    if index is not None:
        if entity_type is not None:
            entry = index.lookup(entity_type, surface_form)
            if entry is not None:
                return _entry_to_result(surface_form, entry, path="fast")
        else:
            matches = index.lookup_any_type(surface_form)
            if matches:
                return _entry_to_result(surface_form, matches[0], path="fast")

    # Slow path: delegate to the existing database substring matcher.
    slow = _try_slow_path(surface_form)
    if slow is not None:
        return slow

    reason = (
        load_error
        if (index is None and load_error)
        else f"no match in dictionary or database for {surface_form!r}"
    )
    return fast_miss(surface_form, reason=reason)


# ---------------------------------------------------------------------------
# Ambiguity-aware lookup (shared by harmonized_search + MCP HITL gate)
# ---------------------------------------------------------------------------


def detect_ambiguity(
    surface_form: str,
    *,
    entity_type: EntityType | None = None,
) -> tuple[LookupResult, list[dict[str, object]]]:
    """Resolve a surface form AND detect multi-IRI ambiguity.

    Returns ``(primary, candidates)``. ``primary`` is the
    :class:`LookupResult` from :func:`lookup_entity` — the first-match
    optimistic answer. ``candidates`` is a list of distinct canonical
    entries the surface form maps to:

    - ``len(candidates) <= 1`` — the term is unambiguous; the caller
      may proceed with ``primary``.
    - ``len(candidates) > 1`` — the term resolves to multiple distinct
      canonical IRIs. The caller MUST surface the candidate list to
      the user and stop (do NOT pick one silently). This is the
      structural HITL gate the harmonized_search workflow established.

    Ambiguity is detected in two phases:

    1. The dictionary's ``ambiguous_surface_forms`` table — the
       authoritative source for known multi-IRI conflicts captured at
       build time (e.g. RSV → 6 candidates).
    2. Fall-through via :meth:`DictionaryIndex.lookup_any_type` — for
       conflicts the build pass missed (returns distinct entries
       across entity types for the same surface form).

    IRI input (``http(s)://...``) is by construction unambiguous; the
    caller already resolved disambiguation. The ambiguity check is
    skipped for IRI input.

    When the dictionary index is unavailable, the function degrades
    gracefully to ``(primary, [])`` rather than raising — the caller
    proceeds with the optimistic single-match answer (this matches the
    pre-2026-06-09 behavior for backwards compatibility).
    """
    primary = lookup_entity(surface_form, entity_type=entity_type)

    if surface_form.startswith(("http://", "https://")):
        return primary, []

    try:
        index_obj, _err = get_dictionary_index()
    except Exception:  # pragma: no cover — defensive against loader failure
        return primary, []

    if index_obj is None:
        return primary, []

    surface_norm = " ".join(surface_form.casefold().split())
    candidate_iris: list[str] = []
    seen_iris: set[str] = set()

    try:
        amb_rows = index_obj.lookup_ambiguous_surface_forms(
            surface_form=surface_norm,
            limit=50,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort ambiguity check
        log.warning(
            "detect_ambiguity: lookup_ambiguous_surface_forms failed: %s",
            exc,
        )
        amb_rows = []

    for row in amb_rows:
        for iri_key in ("winning_canonical_iri", "alternative_canonical_iri"):
            iri = row.get(iri_key)
            if iri and iri not in seen_iris:
                seen_iris.add(iri)
                candidate_iris.append(iri)

    if not candidate_iris:
        try:
            matches: list[DictionaryEntry] = index_obj.lookup_any_type(surface_form)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "detect_ambiguity: lookup_any_type failed: %s",
                exc,
            )
            matches = []
        for entry in matches:
            if entry.canonical_iri not in seen_iris:
                seen_iris.add(entry.canonical_iri)
                candidate_iris.append(entry.canonical_iri)

    if len(candidate_iris) <= 1:
        return primary, []

    candidates: list[dict[str, object]] = []
    for iri in candidate_iris:
        try:
            entry = index_obj.lookup_by_iri(iri)
        except Exception:  # noqa: BLE001
            entry = None
        if entry is not None:
            candidates.append(
                {
                    "canonical_iri": entry.canonical_iri,
                    "canonical_label": entry.canonical_label,
                    "canonical_ontology": entry.ontology.value,
                    "confidence": entry.confidence,
                }
            )
        else:
            candidates.append(
                {
                    "canonical_iri": iri,
                    "canonical_label": None,
                    "canonical_ontology": None,
                    "confidence": 0.0,
                }
            )

    return primary, candidates


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _entry_to_result(
    surface_form: str, entry: DictionaryEntry, *, path: Literal["fast", "ancestor", "slow"]
) -> LookupResult:
    return LookupResult(
        surface_form=surface_form,
        path=path,
        canonical_iri=entry.canonical_iri,
        canonical_label=entry.canonical_label,
        canonical_ontology=entry.ontology.value,
        confidence=entry.confidence,
        resolution_status=ResolutionStatus.ID_ANCHORED
        if entry.confidence == 1.0
        else ResolutionStatus.OLS_FUZZY,
        synonyms=entry.synonyms,
        evidence=(
            f"dictionary_version={entry.ontology_version}; "
            f"source_records={len(entry.source_records)}"
        ),
    )


def _ancestor_to_result(surface_form: str, ancestor: DictionaryEntry) -> LookupResult:
    """Build a LookupResult for a taxon-hierarchy ancestor match.

    Confidence is the ancestor entry's confidence × 0.9 to signal that
    the match is indirect (caller queried a strain; we matched the species).
    """
    return LookupResult(
        surface_form=surface_form,
        path="ancestor",
        canonical_iri=ancestor.canonical_iri,
        canonical_label=ancestor.canonical_label,
        canonical_ontology=ancestor.ontology.value,
        confidence=round(ancestor.confidence * 0.9, 4),
        resolution_status=ResolutionStatus.ID_ANCHORED
        if ancestor.confidence == 1.0
        else ResolutionStatus.OLS_FUZZY,
        synonyms=ancestor.synonyms,
        evidence=(
            f"NCBITaxon ancestor match; queried={surface_form!r}; "
            f"ancestor_iri={ancestor.canonical_iri}; "
            f"dictionary_version={ancestor.ontology_version}"
        ),
    )


def _try_slow_path(surface_form: str) -> LookupResult | None:
    """Fall back to the existing database substring resolver.

    Uses ``database.resolve_entity`` which returns:
    ``{"query": ..., "matches": {"pathogens": [...], "vaccines": [...], ...},
       "identifiers": {"ncbi_taxonomy_ids": [...], ...}, ...}``

    Returns None when the database store isn't loaded or no matches exist.
    Keeps this module importable in environments without APECX_DATA_ROOT set.
    """
    try:
        from apecx_integration.mcp_surface.data import database as _db  # noqa: PLC0415

        store, _ = _db.get_store()
        if store is None:
            return None
        result = _db.resolve_entity(store, surface_form)
        matches = result.get("matches", {})
        identifiers = result.get("identifiers", {})

        # Prefer pathogen matches (highest NCBI Taxonomy coverage).
        pathogens = matches.get("pathogens") or []
        if pathogens:
            best = pathogens[0]
            ncbi_ids = identifiers.get("ncbi_taxonomy_ids", [])
            ncbi_iri = (
                f"http://purl.obolibrary.org/obo/NCBITaxon_{ncbi_ids[0]}" if ncbi_ids else None
            )
            return LookupResult(
                surface_form=surface_form,
                path="slow",
                canonical_iri=ncbi_iri,
                canonical_label=best.get("name"),
                canonical_ontology="ncbitaxon" if ncbi_iri else None,
                confidence=0.3,
                resolution_status=ResolutionStatus.OLS_FUZZY,
                synonyms=(),
                evidence=(f"database substring match (pathogen); ncbi_ids={ncbi_ids[:3]}"),
            )

        # Fall back to vaccine matches.
        vaccines = matches.get("vaccines") or []
        if vaccines:
            best = vaccines[0]
            violin_ids = identifiers.get("violin_vaccine_ids", [])
            return LookupResult(
                surface_form=surface_form,
                path="slow",
                canonical_iri=None,
                canonical_label=best.get("name"),
                canonical_ontology=None,
                confidence=0.2,
                resolution_status=ResolutionStatus.OLS_FUZZY,
                synonyms=(),
                evidence=(f"database substring match (vaccine); violin_ids={violin_ids[:3]}"),
            )

        return None
    except Exception as exc:
        log.debug("slow path unavailable: %s", exc)
        return None
