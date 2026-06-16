"""Stateless PubMed harvest + term-build helpers.

Extracted from ``PubMedHarvesterStep`` so the synthesis pipeline's
fan-in step (``SynthesisContextAssemblyStep``) can drive a PubMed
harvest WITHOUT instantiating a step instance via ``object.__new__`` —
that pattern was a documented corner cut violating the nanobrain rule
that step instances must always be created through ``from_config``.

These helpers are pure modulo the network call ``harvest`` makes:
output depends only on arguments + the upstream eUtils state. The
``owner_name`` parameter only flows into log lines so an operator can
correlate WARNINGs back to the calling step.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


_AUTHORS_CAP = 25

# PubMed eSearch treats these barewords as boolean operators — never emit them
# as search terms. A token like "and" inside a concept list would silently turn
# the OR-group into an unintended boolean.
_PUBMED_BOOLEAN = frozenset({"and", "or", "not"})

# Word characters only; splits a free-text query / organism name into tokens.
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def entity_name(entity: Any) -> str | None:
    """Extract a display name from an entity dict or string.

    Returns ``None`` if neither shape carries a usable name; the caller
    filters those out.
    """
    if isinstance(entity, str):
        return entity.strip() or None
    if isinstance(entity, dict):
        name = entity.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def build_term(
    query: str,
    entities: list[Any] | None,
    template: str = "{query}",
    *,
    owner_name: str = "",
) -> str:
    """Apply ``template`` to build a PubMed eSearch term.

    The template can reference ``{query}`` and/or ``{entities}``; the
    entities placeholder is filled with a comma-joined list of names
    from the ``entities`` list (empty string when none).
    """
    names: list[str] = []
    if entities:
        for ent in entities:
            name = entity_name(ent)
            if name:
                names.append(name)
    entities_str = ", ".join(names)
    try:
        return template.format(query=query, entities=entities_str)
    except (KeyError, IndexError) as exc:
        prefix = f"{owner_name}: " if owner_name else ""
        log.warning(
            "%squery_template format failed (%s); falling back to raw query",
            prefix,
            exc,
        )
        return query


def build_focused_term(query: str, *, owner_name: str = "") -> str:
    """Build an ORGANISM-ANCHORED PubMed eSearch term from a free-text query.

    The naive ``{query}`` template hands the entire natural-language sentence to
    eSearch, which ANDs every token — so a verbose query like
    ``"conserved chikungunya structural polyprotein epitopes and structural
    references"`` returns ZERO papers even though thousands exist (the generic
    words ``conserved`` / ``references`` and the bareword ``and`` over-constrain
    the AND). This builder instead anchors on the virus name(s) the resolver
    extracts from the query and OR-groups the remaining concept tokens::

        ("Chikungunya virus") AND (conserved OR structural OR polyprotein OR ...)

    so every hit is on-organism while recall stays high (verified: the CHIKV
    structural-polyprotein query goes 0 → ~1.2k real records).

    Degrade-loud: when no virus name is found in the query, or the resolver is
    unavailable, it FALLS BACK to the raw query — never worse than the prior
    behaviour. The upstream ``data_readiness`` stage still names a 0-publication
    result as a coverage gap, so a genuine miss stays visible.
    """
    try:
        from apecx_integration.agents.globus_search import taxonomy_resolver

        organisms = [
            o.strip()
            for o in (taxonomy_resolver.extract_virus_names(query) or [])
            if isinstance(o, str) and o.strip()
        ]
    except Exception as exc:  # resolver optional / import-time failure → raw query
        prefix = f"{owner_name}: " if owner_name else ""
        log.warning("%sorganism extraction failed (%s); using raw query", prefix, exc)
        return query

    if not organisms:
        # No anchor in the query text → keep current behaviour (raw query).
        return query

    # Concept tokens: query words NOT already part of an organism name, >= 3 chars,
    # and not a PubMed boolean operator. Dedup case-insensitively, preserve order.
    organism_words = {w.lower() for org in organisms for w in _WORD_RE.findall(org)}
    seen: set[str] = set()
    concepts: list[str] = []
    for raw in _WORD_RE.findall(query):
        w = raw.lower()
        if len(w) < 3 or w in _PUBMED_BOOLEAN or w in organism_words or w in seen:
            continue
        seen.add(w)
        concepts.append(raw)

    if len(organisms) > 1:
        anchor = "(" + " OR ".join(f'"{o}"' for o in organisms) + ")"
    else:
        anchor = f'"{organisms[0]}"'

    if not concepts:
        return anchor
    return f"{anchor} AND ({' OR '.join(concepts)})"


def container_to_dict(container: Any) -> dict[str, Any]:
    """Project a ``PubMedContainer`` into the synthesizer's
    publication-dict shape.

    Resilient to missing fields — DataCite is permissive about which
    optional fields are populated, and PubMed XML records vary widely
    in completeness.
    """
    title = ""
    titles = getattr(container, "titles", None) or []
    if titles:
        title = getattr(titles[0], "title", "") or ""

    # Author list is hard-capped. Real-world papers can carry 1000+
    # consortium authors; uncapped that bloats RAM and the LLM prompt
    # for no benefit. Truncation is signaled with an "et al." marker so
    # the LLM doesn't claim the list is exhaustive.
    authors: list[str] = []
    creators = getattr(container, "creators", None) or []
    creators_total = len(creators)
    for creator in creators[:_AUTHORS_CAP]:
        name = getattr(creator, "name", None)
        if not name:
            family = getattr(creator, "familyName", None)
            given = getattr(creator, "givenName", None)
            if family and given:
                name = f"{family}, {given}"
            elif family:
                name = family
            elif given:
                name = given
        if name:
            authors.append(name)
    if creators_total > _AUTHORS_CAP:
        authors.append(f"et al. ({creators_total - _AUTHORS_CAP} more)")

    year = getattr(container, "publicationYear", None) or ""
    if not year:
        for date in getattr(container, "dates", None) or []:
            date_str = getattr(date, "date", None)
            if date_str:
                year = date_str[:4]
                break

    publisher = getattr(container, "publisher", None)
    journal = getattr(publisher, "name", "") if publisher else ""

    doi = ""
    identifier = getattr(container, "identifier", None)
    if identifier is not None:
        doi = getattr(identifier, "identifier", "") or ""

    pmid = ""
    for alt in getattr(container, "alternateIdentifiers", None) or []:
        if getattr(alt, "alternateIdentifierType", "") == "PMID":
            pmid = getattr(alt, "alternateIdentifier", "") or ""
            if pmid:
                break

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": str(year) if year else "",
        "journal": journal,
        "pmid": pmid,
    }


async def harvest(term: str, *, max_papers: int = 0) -> list[dict[str, Any]]:
    """Run the PubMed eSearch → eFetch chain for ``term``.

    Args:
        term: eSearch query string.
        max_papers: Cap on PMIDs collected. ``<= 0`` means "no limit —
            collect every PMID eSearch returns" (PubMed eSearch can return
            10k+; the focused organism-anchored term keeps this bounded in
            practice). A positive value stops early.

    Returns:
        List of publication dicts via ``container_to_dict``.

    Raises:
        Anything ``apecx_harvesters`` raises — caller is expected to
        wrap in try/except. The synthesis pipeline catches via
        ``asyncio.gather(return_exceptions=True)``.
    """
    # Imports are local so the wrapper module loads even when the
    # ``apecx_harvesters`` extra is not installed (the import error then
    # surfaces only when this function actually runs).
    from apecx_harvesters.loaders.pubmed.retrieve import PubMedHarvester
    from apecx_harvesters.loaders.pubmed.search import search as pubmed_search

    pmids: list[str] = []
    async for pmid in pubmed_search(term):
        pmids.append(pmid)
        if max_papers > 0 and len(pmids) >= max_papers:
            break

    if not pmids:
        return []

    harvester = PubMedHarvester()
    publications: list[dict[str, Any]] = []
    async for result in harvester.iter_results(pmids):
        if result.ok and result.record is not None:
            publications.append(container_to_dict(result.record))
        elif result.error:
            log.warning(
                "PubMed: failed to retrieve PMID %s: %s",
                result.id,
                result.error,
            )
    return publications


__all__ = [
    "build_focused_term",
    "build_term",
    "container_to_dict",
    "entity_name",
    "harvest",
]
