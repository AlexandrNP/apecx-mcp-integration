"""Per-entity-type resolvers backed by OLS (or, for genes, source-row data).

Each resolver takes an entity record (a dict from a VIOLIN/BV-BRC row),
extracts the relevant fields, and produces a :class:`ResolutionResult`.

The resolver uses the **anchor-mode** path when the row carries an
existing authoritative ID (per M1 measurements, this is the dominant
case: 84-97% of VIOLIN's pathogen/vaccine rows).  Falls back to
**search-mode** for un-IDd rows.

:class:`GeneResolver` is the exception: NCBI Gene is not hosted in EBI OLS,
so it constructs ``identifiers.org`` IRIs directly from ``NCBI_Gene_ID``
without any OLS calls.  See the class docstring for details.
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

    async def _resolve_by_iri(
        self,
        iri: str,
        *,
        fallback_label: str | None = None,
        fallback_synonyms: tuple[str, ...] = (),
    ) -> ResolutionResult:
        """Anchor-mode lookup for an existing IRI.

        When OLS returns a term, use the ontology-supplied label + synonyms.
        When OLS returns 404 (deprecated taxon ID, missing branch, etc.) and
        the caller supplied ``fallback_label``, build an entry from the
        database-side label so the dictionary still resolves the user-typed
        name. This is the "database-specific entries" path — VIOLIN's anchor
        ID is the source of truth even when OLS has moved on. ``fallback_synonyms``
        merge with any OLS-supplied synonyms (or stand alone on 404).
        """
        term = await self._ols.get_term(self.ontology, iri)
        if term is None:
            if fallback_label:
                merged_synonyms = self._merge_synonyms((), fallback_synonyms)
                return ResolutionResult(
                    canonical_iri=iri,
                    canonical_label=fallback_label,
                    canonical_ontology=self.ontology,
                    synonyms=merged_synonyms,
                    # Still ID-anchored: the caller supplied the IRI from the
                    # source database, so the IRI is trusted. The label just
                    # comes from the database rather than the ontology.
                    resolution_status=ResolutionStatus.ID_ANCHORED,
                    resolution_confidence=1.0,
                    dictionary_version=self._dictionary_version,
                )
            return self._unresolved(reason=f"OLS get_term returned None for {iri}")
        label = OLSClient.extract_label(term) or iri
        ols_synonyms = OLSClient.extract_synonyms(term)
        merged_synonyms = self._merge_synonyms(ols_synonyms, fallback_synonyms)
        # If the OLS label and the fallback label differ, register the
        # fallback label as an extra synonym so user-typed names from the
        # source database still hit the fast path.
        if fallback_label and fallback_label.strip() and fallback_label != label:
            merged_synonyms = self._merge_synonyms(merged_synonyms, (fallback_label,))
        return ResolutionResult(
            canonical_iri=iri,
            canonical_label=label,
            canonical_ontology=self.ontology,
            synonyms=merged_synonyms,
            resolution_status=ResolutionStatus.ID_ANCHORED,
            resolution_confidence=1.0,
            dictionary_version=self._dictionary_version,
        )

    @staticmethod
    def _merge_synonyms(
        primary: tuple[str, ...] | list, extra: tuple[str, ...] | list
    ) -> tuple[str, ...]:
        """Concat two synonym lists, dedupe, preserve order."""
        seen: dict[str, None] = {}
        for s in list(primary) + list(extra):
            if isinstance(s, str) and s and s not in seen:
                seen[s] = None
        return tuple(seen)

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
        # Compute the database-side label + extra synonyms once; reuse for
        # any anchor-mode lookup so deprecated NCBI IDs still resolve via
        # the VIOLIN-supplied label (database-specific entries).
        db_label = (
            record.get("Pathogen") or record.get("genome_name") or record.get("genome.genome_name")
        )
        db_label_str = db_label if isinstance(db_label, str) and db_label.strip() else None
        # BV-BRC genome_id is itself a useful surface form: scientists may
        # type "37124.6497" expecting to land on the species. Add it as a
        # synonym so the precision filter still resolves correctly. The
        # genome_id IS uniquely the species (the ".N" suffix is a strain
        # serial), so it's a legitimate species-level synonym.
        db_extra: list[str] = []
        gid = record.get("genome_id") or record.get("genome.genome_id")
        if isinstance(gid, str) and gid.strip():
            db_extra.append(gid.strip())
        # Genus / Species / Family are NOT synonyms of the species — they
        # name PARENT taxa (Henipavirus is a genus containing many species,
        # Coronaviridae a family containing many genera). Treating them as
        # synonyms breaks the strict-hierarchy contract: a query for
        # "Coronaviridae" should match the family taxon (and therefore all
        # its descendants), not collapse onto a single child species. They
        # remain available as ``context_terms`` for OLS disambiguation
        # below, but never as direct synonyms.
        db_extra_tuple = tuple(db_extra)

        # Anchor mode: existing taxonomy ID column.
        ncbi_id = record.get("NCBI_Taxonomy_ID")
        iri = normalize_iri(ncbi_id, prefix=self.iri_prefix)
        if iri is not None:
            return await self._resolve_by_iri(
                iri,
                fallback_label=db_label_str,
                fallback_synonyms=db_extra_tuple,
            )

        # BV-BRC implicit taxon (genome_id like "37124.6497").
        if isinstance(gid, str) and "." in gid:
            head = gid.split(".", 1)[0]
            implicit = normalize_iri(head, prefix=self.iri_prefix)
            if implicit is not None:
                return await self._resolve_by_iri(
                    implicit,
                    fallback_label=db_label_str,
                    fallback_synonyms=db_extra_tuple,
                )

        # Search-mode fallback.
        if db_label_str is None:
            return self._unresolved(reason="no Pathogen/genome_name field")
        context = [str(record.get(k, "")) for k in ("Genus", "Species", "Family")]
        return await self._resolve_by_search(db_label_str, context_terms=context)


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
        # Database-side label captured once for both anchor + search paths.
        db_label = record.get("Vaccine_Name") or record.get("Vaccine")
        db_label_str = db_label if isinstance(db_label, str) and db_label.strip() else None

        vo_id = record.get("Vaccine_Ontology_ID")
        iri = normalize_iri(vo_id, prefix=self.iri_prefix)
        if iri is not None:
            # Same pattern as PathogenResolver: VIOLIN's Vaccine_Ontology_ID
            # is the source of truth for VIOLIN data; if VO has retired or
            # restructured an entry, the fallback_label preserves the
            # operator-typed name as canonical.
            return await self._resolve_by_iri(
                iri,
                fallback_label=db_label_str,
                fallback_synonyms=(),
            )

        if db_label_str is None:
            return self._unresolved(reason="no Vaccine/Vaccine_Name field")
        context = [str(record.get(k, "")) for k in ("Type", "Antigen")]
        return await self._resolve_by_search(db_label_str, context_terms=context)


class DiseaseResolver(_ResolverBase):
    """VIOLIN ``Disease`` column on Pathogen_Information rows.

    No native DOID column in VIOLIN snapshot — search-only path. The
    fallback_label kwarg on _resolve_by_iri is unused here because there
    is no anchor-mode IRI; if VIOLIN ever adds a DOID_id column, the
    PathogenResolver pattern applies trivially.
    """

    entity_type = EntityType.DISEASE
    ontology = OntologyName.DOID
    iri_prefix = "DOID_"

    async def resolve(self, record: EntityRecord) -> ResolutionResult:
        label = record.get("Disease")
        if not isinstance(label, str) or not label.strip():
            return self._unresolved(reason="no Disease field")
        return await self._resolve_by_search(label)


# ---------------------------------------------------------------------------
# Gene-specific helpers
# ---------------------------------------------------------------------------

_IDENTIFIERS_ORG_NCBIGENE = "http://identifiers.org/ncbigene/"

# Matches "Ifng (Interferon gamma)" — captures symbol + long name.
_GENE_NAME_PARENS_RE = re.compile(r"^(.+?)\s+\((.+)\)\s*$")

# Matches "SodC from B. abortus strain 2308" — captures symbol before "from".
_GENE_NAME_FROM_RE = re.compile(r"^(.+?)\s+from\s+.+$", re.IGNORECASE)


def _build_ncbigene_iri(value: str | int | float | None) -> str | None:
    """Construct an identifiers.org NCBI Gene IRI from a gene ID value.

    Handles the pandas float-string issue (NCBI_Gene_ID read as float64,
    producing "15978.0") the same way normalize_iri handles NCBITaxon.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.isdigit():
        return f"{_IDENTIFIERS_ORG_NCBIGENE}{text}"
    try:
        float_val = float(text)
        int_val = int(float_val)
        if float_val == int_val and int_val > 0:
            return f"{_IDENTIFIERS_ORG_NCBIGENE}{int_val}"
    except ValueError:
        pass
    return None


def _gene_label_from_name(gene_name: str) -> str:
    """Extract the primary label (gene symbol) from a Gene_Name column value.

    Examples:
    - "Ifng (Interferon gamma)" -> "Ifng"
    - "SodC from B. abortus strain 2308" -> "SodC"
    - "IglC" -> "IglC"
    """
    if not gene_name:
        return gene_name
    m = _GENE_NAME_PARENS_RE.match(gene_name)
    if m:
        return m.group(1).strip()
    m = _GENE_NAME_FROM_RE.match(gene_name)
    if m:
        return m.group(1).strip()
    return gene_name.strip()


def _extract_gene_synonyms(gene_name: str) -> tuple[str, ...]:
    """Extract all useful synonym forms from a Gene_Name column value.

    Always includes the full Gene_Name string.  Additionally extracts:
    - The bare symbol and the parenthesised long name for "symbol (long)" format.
    - The bare symbol for "symbol from Organism" format.
    """
    if not gene_name or not gene_name.strip():
        return ()
    gene_name = gene_name.strip()
    synonyms: set[str] = {gene_name}

    m = _GENE_NAME_PARENS_RE.match(gene_name)
    if m:
        synonyms.add(m.group(1).strip())
        synonyms.add(m.group(2).strip())
        return tuple(sorted(synonyms))

    m = _GENE_NAME_FROM_RE.match(gene_name)
    if m:
        synonyms.add(m.group(1).strip())

    return tuple(sorted(synonyms))


class GeneResolver(_ResolverBase):
    """VIOLIN ``Gene_Information`` rows.

    NCBI Gene is **not** hosted in EBI OLS, so this resolver constructs
    canonical IRIs and synonyms purely from source-row data — no OLS
    calls are made.

    Reads:

    - ``NCBI_Gene_ID`` — VIOLIN column (73.5% fill rate per M1).
      Produces ``http://identifiers.org/ncbigene/{id}`` IRI (identifiers.org
      standard for NCBI Gene).
    - ``Gene_Name`` — free-text label used for both the canonical label and
      synonym extraction.  Names follow two patterns:
        - ``symbol (long name)`` — e.g. "Ifng (Interferon gamma)"
        - ``symbol from Organism`` — e.g. "SodC from B. abortus strain 2308"
    - ``Organism`` — used as disambiguation context (not yet wired into
      search, since there is no OLS search fallback).

    Rows without ``NCBI_Gene_ID`` are marked UNRESOLVED.  Unlike pathogen
    or vaccine resolvers, there is no OLS search fallback because no OLS
    endpoint covers all organisms' gene identifiers.

    OLS client: accepted in the constructor to satisfy ``_ResolverBase``'s
    interface but never called.
    """

    entity_type = EntityType.GENE
    ontology = OntologyName.NCBIGENE
    iri_prefix = "ncbigene_"  # unused — IRI constructed from identifiers.org base

    async def resolve(self, record: EntityRecord) -> ResolutionResult:
        gene_id = record.get("NCBI_Gene_ID")
        iri = _build_ncbigene_iri(gene_id)
        if iri is None:
            return self._unresolved(reason="no NCBI_Gene_ID")

        gene_name = record.get("Gene_Name") or ""
        if not isinstance(gene_name, str):
            gene_name = str(gene_name)
        label = _gene_label_from_name(gene_name) if gene_name else iri
        synonyms = _extract_gene_synonyms(gene_name)

        return ResolutionResult(
            canonical_iri=iri,
            canonical_label=label,
            canonical_ontology=self.ontology,
            synonyms=synonyms,
            resolution_status=ResolutionStatus.ID_ANCHORED,
            resolution_confidence=1.0,
            dictionary_version=self._dictionary_version,
        )
