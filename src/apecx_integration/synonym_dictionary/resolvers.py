"""Per-entity-type resolvers backed by OLS.

Each resolver takes an entity record (a dict from a VIOLIN/BV-BRC row),
extracts the relevant fields, and produces a :class:`ResolutionResult`.

The resolver uses the **anchor-mode** path when the row carries an
existing authoritative ID (per M1 measurements, this is the dominant
case: 84-97% of VIOLIN's pathogen/vaccine rows).  Falls back to
**search-mode** for un-IDd rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from apecx_integration.synonym_dictionary.enums import (
    EntityType,
    OntologyName,
    ResolutionStatus,
)
from apecx_integration.synonym_dictionary.ols_client import OLSClient
from apecx_integration.synonym_dictionary.schema import ResolutionResult
from apecx_integration.synonym_dictionary.transform import EntityRecord

# OBO purl prefix used to convert bare ontology IDs (e.g. "VO_0000122") into
# canonical IRIs.
_OBO_PURL = "http://purl.obolibrary.org/obo/"

# Recognise either a bare OBO id (NCBITaxon_37124) or a full IRI.
_BARE_ID_RE = re.compile(r"^[A-Za-z]+_\d+$")


def normalize_iri(value: str | int | float | None, *, prefix: str) -> str | None:
    """Convert a column value (which might be a bare numeric ID, a bare
    OBO id, or a full IRI) into a full OBO IRI.  Returns None when the
    input cannot be coerced.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if _BARE_ID_RE.match(text):
        return _OBO_PURL + text
    # Numeric-only — combine with the supplied prefix (e.g. "NCBITaxon_").
    # Handles both plain integers ("10298") and float-strings from pandas
    # ("10298.0" — numpy.float64 columns read as "10298.0" via str()).
    if text.isdigit():
        return f"{_OBO_PURL}{prefix}{text}"
    try:
        float_val = float(text)
        int_val = int(float_val)
        if float_val == int_val and int_val > 0:
            return f"{_OBO_PURL}{prefix}{int_val}"
    except ValueError:
        pass
    return None


class _ResolverBase:
    """Common resolver behaviour: build a :class:`ResolutionResult` from
    an OLS term payload."""

    entity_type: EntityType
    ontology: OntologyName
    iri_prefix: str  # e.g. "NCBITaxon_" — used by ``normalize_iri``

    def __init__(self, ols: OLSClient, *, dictionary_version: str) -> None:
        self._ols = ols
        self._dictionary_version = dictionary_version

    async def _resolve_by_iri(self, iri: str) -> ResolutionResult:
        term = await self._ols.get_term(self.ontology, iri)
        if term is None:
            return self._unresolved(reason=f"OLS get_term returned None for {iri}")
        label = OLSClient.extract_label(term) or iri
        synonyms = OLSClient.extract_synonyms(term)
        return ResolutionResult(
            canonical_iri=iri,
            canonical_label=label,
            canonical_ontology=self.ontology,
            synonyms=synonyms,
            resolution_status=ResolutionStatus.ID_ANCHORED,
            resolution_confidence=1.0,
            dictionary_version=self._dictionary_version,
        )

    async def _resolve_by_search(
        self,
        query: str,
        *,
        context_terms: Iterable[str] = (),
    ) -> ResolutionResult:
        """Free-text search fallback.  ``context_terms`` is currently used
        only as a tie-breaker when multiple candidates match the same
        normalized query — see ``_disambiguate``."""
        if not query.strip():
            return self._unresolved(reason="empty query")
        docs = await self._ols.search(query, self.ontology, rows=5)
        if not docs:
            return self._unresolved(reason=f"OLS search returned no docs for {query!r}")

        chosen, status, confidence = self._disambiguate(query, docs, context_terms)
        if chosen is None:
            return self._unresolved(reason=f"disambiguation declined for {query!r}")

        iri = chosen.get("iri")
        if not isinstance(iri, str):
            return self._unresolved(reason=f"OLS doc lacked IRI: {chosen}")

        # Re-fetch the term to get the full synonym set; OLS search returns
        # only a label and the IRI.
        term = await self._ols.get_term(self.ontology, iri)
        label = OLSClient.extract_label(term) or chosen.get("label") or iri
        synonyms = OLSClient.extract_synonyms(term)
        return ResolutionResult(
            canonical_iri=iri,
            canonical_label=label,
            canonical_ontology=self.ontology,
            synonyms=synonyms,
            resolution_status=status,
            resolution_confidence=confidence,
            dictionary_version=self._dictionary_version,
        )

    def _disambiguate(
        self,
        query: str,
        docs: list[dict[str, Any]],
        context_terms: Iterable[str],
    ) -> tuple[dict[str, Any] | None, ResolutionStatus, float]:
        """Pick the best OLS doc out of ``docs`` for the given query.

        Heuristic:

        - Exact case-insensitive label match -> ``OLS_EXACT``, conf 0.9.
        - Otherwise, take the highest-scored doc (OLS already orders by
          score) and emit ``OLS_FUZZY`` with confidence proportional to
          rank position (top doc 0.7, next 0.5, etc.).
        - Context terms can break ties: if a context term appears in a
          doc's label, prefer that doc.
        """
        ctx_norm = {c.casefold() for c in context_terms if c}
        q_norm = query.casefold().strip()

        # Exact label match (case-insensitive).
        for d in docs:
            if (d.get("label") or "").casefold() == q_norm:
                return d, ResolutionStatus.OLS_EXACT, 0.9

        # Context-term tiebreak.
        if ctx_norm:
            for d in docs:
                label = (d.get("label") or "").casefold()
                if any(ct in label for ct in ctx_norm):
                    return d, ResolutionStatus.OLS_FUZZY, 0.7

        # Fall back to the highest-ranked doc.
        if docs:
            return docs[0], ResolutionStatus.OLS_FUZZY, 0.5

        return None, ResolutionStatus.UNRESOLVED, 0.0

    def _unresolved(self, *, reason: str = "") -> ResolutionResult:
        # ``reason`` is logged at the call site rather than persisted —
        # the on-disk artifact records only the status, not the message.
        if reason:
            # Lightweight debug visibility; no logger spam by default.
            pass
        return ResolutionResult(
            canonical_iri=None,
            canonical_label=None,
            canonical_ontology=None,
            synonyms=(),
            resolution_status=ResolutionStatus.UNRESOLVED,
            resolution_confidence=0.0,
            dictionary_version=self._dictionary_version,
        )

    async def resolve(self, record: EntityRecord) -> ResolutionResult:
        """Override in subclasses."""
        raise NotImplementedError


class PathogenResolver(_ResolverBase):
    """VIOLIN ``Pathogen_Information`` rows (and BV-BRC genome rows).

    Reads:

    - ``NCBI_Taxonomy_ID`` — VIOLIN column with the existing ID (96.8% fill).
    - ``genome_id`` — BV-BRC column whose first dot-separated token is
      the implicit NCBI taxon (e.g. ``37124.6497``).
    - ``Pathogen`` — VIOLIN free-text label, used for search-mode fallback.
    - ``Genus``, ``Species``, ``Family`` — used as disambiguation context.
    """

    entity_type = EntityType.PATHOGEN
    ontology = OntologyName.NCBITAXON
    iri_prefix = "NCBITaxon_"

    async def resolve(self, record: EntityRecord) -> ResolutionResult:
        # Anchor mode: existing taxonomy ID column.
        ncbi_id = record.get("NCBI_Taxonomy_ID")
        iri = normalize_iri(ncbi_id, prefix=self.iri_prefix)
        if iri is not None:
            return await self._resolve_by_iri(iri)

        # BV-BRC implicit taxon (genome_id like "37124.6497").
        genome_id = record.get("genome_id") or record.get("genome.genome_id")
        if isinstance(genome_id, str) and "." in genome_id:
            head = genome_id.split(".", 1)[0]
            implicit = normalize_iri(head, prefix=self.iri_prefix)
            if implicit is not None:
                return await self._resolve_by_iri(implicit)

        # Search-mode fallback.
        label = (
            record.get("Pathogen") or record.get("genome_name") or record.get("genome.genome_name")
        )
        if not isinstance(label, str):
            return self._unresolved(reason="no Pathogen/genome_name field")
        context = [str(record.get(k, "")) for k in ("Genus", "Species", "Family")]
        return await self._resolve_by_search(label, context_terms=context)


class VaccineResolver(_ResolverBase):
    """VIOLIN ``Vaccine_Information`` rows.

    Reads:

    - ``Vaccine_Ontology_ID`` — VIOLIN column with the existing VO id (84.4% fill).
    - ``Vaccine_Name`` / ``Vaccine`` — free-text label for search-mode fallback.
    - ``Type``, ``Antigen`` — disambiguation context.
    """

    entity_type = EntityType.VACCINE
    ontology = OntologyName.VO
    iri_prefix = "VO_"

    async def resolve(self, record: EntityRecord) -> ResolutionResult:
        vo_id = record.get("Vaccine_Ontology_ID")
        iri = normalize_iri(vo_id, prefix=self.iri_prefix)
        if iri is not None:
            return await self._resolve_by_iri(iri)

        label = record.get("Vaccine_Name") or record.get("Vaccine")
        if not isinstance(label, str):
            return self._unresolved(reason="no Vaccine/Vaccine_Name field")
        context = [str(record.get(k, "")) for k in ("Type", "Antigen")]
        return await self._resolve_by_search(label, context_terms=context)


class DiseaseResolver(_ResolverBase):
    """VIOLIN ``Disease`` column on Pathogen_Information rows."""

    entity_type = EntityType.DISEASE
    ontology = OntologyName.DOID
    iri_prefix = "DOID_"

    async def resolve(self, record: EntityRecord) -> ResolutionResult:
        # No native DOID column in VIOLIN snapshot — search-only.
        label = record.get("Disease")
        if not isinstance(label, str) or not label.strip():
            return self._unresolved(reason="no Disease field")
        return await self._resolve_by_search(label)
