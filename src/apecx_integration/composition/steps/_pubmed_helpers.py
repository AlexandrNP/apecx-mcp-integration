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
from typing import Any

log = logging.getLogger(__name__)


_AUTHORS_CAP = 25


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


async def harvest(term: str, *, max_papers: int) -> list[dict[str, Any]]:
    """Run the PubMed eSearch → eFetch chain for ``term``.

    Args:
        term: eSearch query string.
        max_papers: Hard cap on PMIDs collected (PubMed eSearch can
            return 10k+; we stop early).

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
        if len(pmids) >= max_papers:
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
    "build_term",
    "container_to_dict",
    "entity_name",
    "harvest",
]
