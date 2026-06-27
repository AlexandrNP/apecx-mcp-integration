"""Taxon-precise structural search over the aggregate Globus index (e74bf12a).

This is the SHARED core for the two structural-search call sites that MUST stay
in lockstep — ``composition/steps/structural_evidence_step.py`` (the workflow leg)
and ``mcp_surface/tools/harmonized_search.py`` (the MCP tool). Both call
:func:`search_one_source` so a query precision change lands in one place.

The problem this solves (verified 2026-06-13 against the live index):
a free-text PDB query for ``"chikungunya envelope"`` (publisher = RCSB PDB)
returns 1162 hits whose top-10 includes a *West Nile virus* structure (``7E4K``)
— other viruses' structures leak in on shared keywords. The fix is to taxon-lock
the query on the only taxon-bearing structural field,
``pdb.polymer_entities.scientific_name``.

Because that field's match is EXACT and case-sensitive while the organism has many
spellings ("Chikungunya virus", "CHIKUNGUNYA VIRUS", "Chikungunya virus strain
S27-African prototype", ...), a single-value filter UNDER-recalls. So we run a
FACET pre-pass to enumerate every spelling present, then a ``match_any`` over the
full set. After: 9-12 hits, all CHIKV-deposited, West Nile excluded.

EMDB records carry no ``scientific_name`` (organism lives only in the
title/description and there is no taxon id), so EMDB is taxon-locked with an
advanced query_string that REQUIRES the species token AND a structural keyword.

Degrade-loud (CC-1 / CC-2): when no species can be resolved for the query, the
search falls back to the original free-text query and attaches a NAMED note
("results not taxon-locked: ...") — never a silent unfiltered dump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Curated taxon_id -> (canonical species name, distinctive lowercase token).
#
# REUSE NOTE (brutal honesty): the design plan points at a shared ``taxon_species``
# table / ``TaxonSpeciesMapStep`` (memory: ``strain_species_normalization_shipped``,
# 2026-06-09). A grep of this branch (``epitope-evidence-workflow``) confirms that
# work is NOT present here — it shipped on the ``multiclade-species`` branch. So we
# carry a small, NCBI-sourced subset covering the EO target viruses. Arbitrary taxa
# outside this set are resolved from the query text ("X virus") instead, and a query
# that resolves nothing degrades loud. Coverage of arbitrary taxa should later route
# through the synonym dictionary's ``lookup_by_iri`` once that table lands here.
_TAXON_SPECIES: dict[int, tuple[str, str]] = {
    37124: ("Chikungunya virus", "chikungunya"),
    11082: ("West Nile virus", "west nile"),
    64320: ("Zika virus", "zika"),
    12637: ("Dengue virus", "dengue"),
}

# Token -> canonical name, derived from the curated map. Lets a query that names a
# known virus WITHOUT the word "virus" (e.g. "chikungunya envelope epitopes") still
# resolve a species.
_TOKEN_NAMES: dict[str, str] = {tok: name for name, tok in _TAXON_SPECIES.values()}

# "<word(s)> virus" / "viruses" → the distinctive part (group 1), e.g.
# "west nile virus" -> "west nile", "chikungunya virus" -> "chikungunya".
# BUG (fixed 2026-06-13): the suffix was ``viruses?`` which parses as "viruse" + optional
# "s" — it matches "viruse"/"viruses" but NEVER the singular "virus", so this regex
# silently never fired on "<X> virus". The intent is "virus" + optional "es" → ``virus(?:es)?``.
_VIRUS_RE = re.compile(
    r"\b([a-z][a-z0-9'-]*(?:\s+[a-z][a-z0-9'-]*){0,3})\s+virus(?:es)?\b",
    re.IGNORECASE,
)

# Generic structural vocabulary used for the EMDB required-keyword AND-clause when
# the query carries no protein/structural residual of its own.
_DEFAULT_STRUCTURAL_KEYWORDS = (
    "envelope",
    "glycoprotein",
    "spike",
    "capsid",
    "structure",
    "protein",
)

_PDB_SCIENTIFIC_NAME_FIELD = "pdb.polymer_entities.scientific_name"
_PUBLISHER_FIELD = "publisher.name"


@dataclass
class SpeciesResolution:
    """Resolved species scoping for a structural query.

    ``terms`` are the lowercase distinctive substrings used to scope the facet,
    filter buckets, and require the taxon token on EMDB. ``names`` are the canonical
    display names (for provenance + the taxon-resolution test). ``note`` names the
    degrade when nothing resolved.
    """

    terms: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class StructuralSearchResult:
    """One source's taxon-precise structural hits plus any NAMED degrade note."""

    hits: list[dict[str, Any]]
    note: str | None
    organisms: list[str] = field(default_factory=list)
    query_used: str = ""


def resolve_species_terms(
    query: str,
    taxon_id: int | str | None = None,
    species_name: str | None = None,
) -> SpeciesResolution:
    """Resolve a query (+ optional NCBI taxon_id / canonical species name) to scoping terms.

    Resolution order: the curated ``taxon_id`` map (when a taxon id is given), then the
    ``species_name`` canonical spelling (resolved upstream by the BV-BRC taxonomy resolver
    for ARBITRARY viruses — e.g. SARS-CoV-2, influenza, HIV — that are NOT in the curated
    map), then "<X> virus" phrases parsed from the query, then curated distinctive tokens
    that literally appear in the query. When nothing resolves, ``note`` is set to a named
    degrade and ``terms``/``names`` are empty.

    The ``species_name`` term is the FULL lowercased canonical name (e.g. "severe acute
    respiratory syndrome coronavirus 2"). PDB deposits use the scientific name, so the facet
    pre-pass matches it as a substring; using the full name keeps the match taxon-precise
    (verified live: it finds SARS-CoV-2 PDB structures and excludes SARS-CoV-1).
    """
    terms: list[str] = []
    names: list[str] = []

    def _add(term: str, name: str) -> None:
        term = term.strip().lower()
        if term and term not in terms:
            terms.append(term)
            names.append(name)

    tid: int | None = None
    if isinstance(taxon_id, int):
        tid = taxon_id
    elif isinstance(taxon_id, str) and taxon_id.strip().isdigit():
        tid = int(taxon_id.strip())
    if tid is not None and tid in _TAXON_SPECIES:
        name, token = _TAXON_SPECIES[tid]
        _add(token, name)

    # Canonical species name from the upstream BV-BRC taxonomy resolver (arbitrary viruses).
    if isinstance(species_name, str) and species_name.strip():
        _add(species_name.strip().lower(), species_name.strip())

    text = query if isinstance(query, str) else ""
    lowered = text.lower()
    # Match "<X> virus" phrases (now that the ``virus(?:es)?`` suffix is fixed — it
    # previously parsed as "viruse"+"s?" and silently never matched singular "virus").
    # Mostly superseded by the upstream BV-BRC taxonomy resolver, but a silent-never-match
    # is exactly the smell we don't ship.
    for match in _VIRUS_RE.finditer(lowered):
        token = match.group(1).strip()
        # The matched phrase "<token> virus" is the canonical display name.
        _add(token, f"{token} virus")

    for token, name in _TOKEN_NAMES.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            _add(token, name)

    if not terms:
        return SpeciesResolution(
            note=(
                "results not taxon-locked: could not resolve a species for "
                f"{query!r} (no NCBI taxon id and no virus name in the query text)."
            )
        )
    return SpeciesResolution(terms=terms, names=names)


def _structural_keyword_tokens(query: str, terms: list[str]) -> list[str]:
    """The query's protein/structural residual: query words minus species words.

    e.g. ``("chikungunya envelope epitopes", ["chikungunya"]) -> ["envelope",
    "epitopes"]``. Returns ``[]`` when the query is only the species name.
    """
    drop: set[str] = {"virus", "viruses"}
    for term in terms:
        drop.update(term.split())
    out: list[str] = []
    for word in re.findall(r"[a-z0-9][a-z0-9'-]*", query.lower()):
        if word not in drop and word not in out:
            out.append(word)
    return out


def enumerate_organisms(
    species_term: str | list[str],
    *,
    publisher: str = "RCSB PDB",
) -> list[str]:
    """Facet pre-pass: every ``scientific_name`` spelling matching a species term.

    Facets ``pdb.polymer_entities.scientific_name`` scoped to records matching the
    species term (publisher-filtered to RCSB PDB) and returns every bucket value
    whose lowercased text CONTAINS the term — so co-deposited hosts (Homo sapiens,
    Mus musculus) and unrelated viruses (Venezuelan equine encephalitis virus) that
    share a record are excluded, while every strain/case spelling of the target
    organism is kept. Order: highest-count spelling first.

    Raises :class:`GlobusSearchUnavailableError` (via the client) on a Globus error.
    """
    from apecx_integration.agents.globus_search import client as globus_client

    terms = [species_term] if isinstance(species_term, str) else list(species_term)
    pub_filter = [{"type": "match_any", "field_name": _PUBLISHER_FIELD, "values": [publisher]}]
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term_l = term.strip().lower()
        if not term_l:
            continue
        buckets = globus_client.facet(
            _PDB_SCIENTIFIC_NAME_FIELD,
            term_l,
            filters=pub_filter,
            size=100,
        )
        for value, _count in buckets:
            if term_l in value.lower() and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def search_one_source(
    query: str,
    source: str,
    publisher: str,
    *,
    taxon_id: int | str | None = None,
    species_name: str | None = None,
    max_results: int = 0,
) -> StructuralSearchResult:
    """Run a taxon-precise structural query for one source (``"pdb"`` / ``"emdb"``).

    PDB: facet-enumerate the organism spellings, then ``match_any`` on
    ``scientific_name`` + the structural keywords as ``q``. EMDB: advanced ``q``
    requiring the taxon token AND a structural keyword (no ``scientific_name``
    field exists). When the species cannot be resolved, OR (PDB) no organism
    spelling is found, degrade to the original free-text query with a NAMED note.

    Each returned hit is tagged ``structural_source = source``. Globus errors
    propagate (as ``GlobusSearchUnavailableError``) for the caller's outage path.
    """
    from apecx_integration.agents.globus_search import client as globus_client

    pub_filter = {"type": "match_any", "field_name": _PUBLISHER_FIELD, "values": [publisher]}
    resolution = resolve_species_terms(query, taxon_id, species_name)

    def _free_text_degrade(note: str) -> StructuralSearchResult:
        hits = globus_client.search(query, max_results=max_results, filters=[pub_filter])
        _tag(hits, source)
        return StructuralSearchResult(hits=hits, note=note, query_used=query)

    if not resolution.terms:
        return _free_text_degrade(resolution.note or "results not taxon-locked.")

    kw_tokens = _structural_keyword_tokens(query, resolution.terms)

    if source == "pdb":
        organisms = enumerate_organisms(resolution.terms, publisher=publisher)
        if not organisms:
            return _free_text_degrade(
                "results not taxon-locked: no PDB organism spelling matched "
                f"{resolution.names!r} in the structural corpus for {query!r}."
            )
        q = " ".join(kw_tokens) if kw_tokens else " ".join(resolution.terms)
        filters = [
            pub_filter,
            {"type": "match_any", "field_name": _PDB_SCIENTIFIC_NAME_FIELD, "values": organisms},
        ]
        hits = globus_client.search(q, max_results=max_results, filters=filters)
        _tag(hits, source)
        return StructuralSearchResult(hits=hits, note=None, organisms=organisms, query_used=q)

    # EMDB: require the taxon token AND a structural keyword in the free text. NOTE: this is a
    # free-text ``q`` (EMDB has no scientific_name field), so the ``note=None`` returned below TRUSTS
    # Globus AND-semantics to keep the hit taxon-relevant — a looser lock than PDB's structured
    # ``match_any`` on scientific_name. Consumers that DROP non-taxon-locked hits (StructuralEvidenceStep)
    # rely on this note=None meaning "taxon-relevant"; if the index ever loosens AND-semantics, an
    # EMDB hit could slip past that guard. PDB (the structured-filter path) has no such caveat.
    kw = kw_tokens or list(_DEFAULT_STRUCTURAL_KEYWORDS)
    taxon_clause = " OR ".join(f'"{t}"' for t in resolution.terms)
    kw_clause = " OR ".join(f'"{w}"' for w in kw)
    q = f"({taxon_clause}) AND ({kw_clause})"
    hits = globus_client.search(q, max_results=max_results, filters=[pub_filter])
    _tag(hits, source)
    return StructuralSearchResult(hits=hits, note=None, query_used=q)


def _tag(hits: list[dict[str, Any]], source: str) -> None:
    for h in hits:
        if isinstance(h, dict):
            h["structural_source"] = source


__all__ = [
    "SpeciesResolution",
    "StructuralSearchResult",
    "resolve_species_terms",
    "enumerate_organisms",
    "search_one_source",
]
