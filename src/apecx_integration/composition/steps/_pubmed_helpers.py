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

import asyncio
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


_AUTHORS_CAP = 25

_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

# PubMed eSearch reachable ceiling for a single query (retstart max 9998), used
# as the retmax when the caller asks for "no cap" (max_papers <= 0).
_ESEARCH_MAX_RETMAX = 9_999

# NCBI allows 3 requests/second without an API key; honour that budget since the
# eFetch that follows this eSearch shares it.
_NCBI_REQUESTS_PER_SECOND = 3.0

# eSearch JSON responses can carry bare ASCII control characters inside string
# values, which strict JSON parsers reject. Strip them before parsing (matches
# apecx_harvesters' own search loader).
_CONTROL_CHARS_RE = re.compile(rb"[\x00-\x1f\x7f]")

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
    pmcid = ""
    for alt in getattr(container, "alternateIdentifiers", None) or []:
        alt_type = getattr(alt, "alternateIdentifierType", "")
        alt_value = getattr(alt, "alternateIdentifier", "") or ""
        if alt_type == "PMID" and not pmid:
            pmid = alt_value
        elif alt_type == "PMCID" and not pmcid:
            # The PMCID is already parsed into the container (loader stores the eFetch
            # <ArticleId IdType="pmc">); it was DROPPED here, which forced full-text
            # retrieval to re-resolve PMID->PMCID. Carry it so fulltext.py can fetch
            # the open-access body directly.
            pmcid = alt_value

    # The abstract — parsed by the loader from <AbstractText> into the DataCite ``descriptions``
    # list — was previously DROPPED here, so every downstream consumer (the synthesizer prompt
    # AND the rendered report the desktop host LLM reads) saw titles only and could not perform
    # real literature analysis. Carry it forward under ``abstract``.
    abstract = ""
    descriptions = getattr(container, "descriptions", None) or []
    if descriptions:
        abstract = getattr(descriptions[0], "description", "") or ""

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": str(year) if year else "",
        "journal": journal,
        "pmid": pmid,
        "pmcid": pmcid,
        "abstract": abstract,
    }


async def _esearch_relevance_pmids(term: str, max_papers: int) -> list[str]:
    """Return PubMed PMIDs for ``term`` in Best Match (relevance) order.

    Issues ONE eSearch GET with ``sort=relevance`` so the top ``max_papers``
    records are the most topically relevant, not the most recent. The default
    eSearch order (no ``sort``) is most-recent-first; for a high-volume organism
    the result set is far larger than ``max_papers``, so older-but-relevant
    records were silently cut before fetch.

    ``max_papers <= 0`` means "no cap": uses the eSearch single-query ceiling
    (``_ESEARCH_MAX_RETMAX``) as ``retmax``.
    """
    import httpx

    retmax = max_papers if max_papers > 0 else _ESEARCH_MAX_RETMAX
    async with httpx.AsyncClient() as client:
        response = await client.get(
            _ESEARCH_URL,
            params={
                "db": "pubmed",
                "term": term,
                "retmode": "json",
                "retmax": retmax,
                "sort": "relevance",
            },
        )
        response.raise_for_status()
        # Respect NCBI's 3 req/s budget shared with the eFetch that follows.
        await asyncio.sleep(1.0 / _NCBI_REQUESTS_PER_SECOND)

    clean = _CONTROL_CHARS_RE.sub(b"", response.content)
    result = json.loads(clean)["esearchresult"]
    if "ERROR" in result:
        raise ValueError(f"PubMed eSearch error: {result['ERROR']}")

    pmids = [str(p) for p in (result.get("idlist") or [])]
    if max_papers > 0:
        pmids = pmids[:max_papers]
    return pmids


async def harvest(term: str, *, max_papers: int = 0) -> list[dict[str, Any]]:
    """Run the PubMed eSearch → eFetch chain for ``term``.

    The eSearch step orders PMIDs by **relevance** (Best Match), so the
    ``max_papers`` cap keeps the most topically-relevant records rather than the
    most recent (see :func:`_esearch_relevance_pmids`).

    Args:
        term: eSearch query string.
        max_papers: Cap on PMIDs collected. ``<= 0`` means "no cap —
            collect up to the eSearch single-query ceiling"
            (``_ESEARCH_MAX_RETMAX``). A positive value caps the list.

    Returns:
        List of publication dicts via ``container_to_dict``.

    Raises:
        Anything ``apecx_harvesters`` or the eSearch request raises — caller is
        expected to wrap in try/except. The synthesis pipeline catches via
        ``asyncio.gather(return_exceptions=True)``.
    """
    # The eFetch harvester import is local so the wrapper module loads even when
    # the ``apecx_harvesters`` extra is not installed (the import error then
    # surfaces only when this function actually runs).
    from apecx_harvesters.loaders.pubmed.retrieve import PubMedHarvester

    pmids = await _esearch_relevance_pmids(term, max_papers)

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
