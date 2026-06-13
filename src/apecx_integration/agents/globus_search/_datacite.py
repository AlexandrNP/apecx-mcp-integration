"""DataCite-aware field extraction for Globus harvested-corpus records.

Every record in the APECx aggregate Globus index (``e74bf12a``) is stored in the
DataCite metadata shape: the human title is at ``content["titles"][0]["title"]``
and the abstract at ``content["descriptions"][0]["description"]`` — NOT at a flat
``content["title"]`` / ``content["abstract"]`` key.

Three renderers (``rag_synthesis.synthesizer._render_globus_results``,
``mcp_surface.tools.harmonized_search._aggregate_served_search``,
``composition.steps.evidence_review_synthesis_step.render_structural_section``)
previously read the flat keys, so EVERY harvested-corpus hit — journal articles
and PDB/EMDB structures alike — rendered as ``(untitled)`` with no abstract. The
records were retrieved and relevance-ranked correctly; their descriptive content
was silently dropped at render time, so a user (or the synthesis LLM) saw bare
record IDs with no meaning. These helpers are the single source of truth for
pulling the real fields, with a flat-key fallback so any normalized record keeps
working.
"""

from __future__ import annotations

from typing import Any


def datacite_title(content: Any) -> str | None:
    """Return the human title of a Globus record, or ``None``.

    Prefers the DataCite ``titles[0].title``; falls back to a flat ``title`` key
    for records normalized by a harvester into the simpler shape.
    """
    if not isinstance(content, dict):
        return None
    titles = content.get("titles")
    if isinstance(titles, list) and titles:
        first = titles[0]
        if isinstance(first, dict) and first.get("title"):
            return str(first["title"])
    flat = content.get("title")
    return str(flat) if flat else None


def datacite_description(content: Any) -> str | None:
    """Return the abstract/description of a Globus record, or ``None``.

    Prefers the DataCite ``descriptions[0].description``; falls back to flat
    ``abstract`` / ``description`` keys.
    """
    if not isinstance(content, dict):
        return None
    descriptions = content.get("descriptions")
    if isinstance(descriptions, list) and descriptions:
        first = descriptions[0]
        if isinstance(first, dict) and first.get("description"):
            return str(first["description"])
    return content.get("abstract") or content.get("description")


def datacite_subjects(content: Any, limit: int = 6) -> list[str]:
    """Return the DataCite ``subjects[].subject`` keyword list (deduped, capped).

    Structural records (PDB/EMDB) carry their most query-relevant terms here
    (e.g. ``['VIRUS LIKE PARTICLE', 'chikungunya', 'alphavirus', 'antibody']``);
    surfacing them gives the synthesis LLM the semantic anchors the bare ID lacks.
    """
    if not isinstance(content, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in content.get("subjects") or []:
        subj = entry.get("subject") if isinstance(entry, dict) else None
        if subj and isinstance(subj, str) and subj not in seen:
            seen.add(subj)
            out.append(subj)
            if len(out) >= limit:
                break
    return out


def datacite_organisms(content: Any) -> list[str]:
    """Return the deposited organism scientific names of a PDB record (deduped).

    RCSB PDB records in the aggregate index carry the source organism of each
    polymer chain at ``content["pdb"]["polymer_entities"][i]["scientific_name"]``
    (e.g. ``"Chikungunya virus"``, plus ``"Homo sapiens"`` for a bound Fab). This
    is the only taxon-bearing field on a structural record — EMDB records have no
    equivalent (organism lives only in their title/description), and neither source
    carries an NCBI taxon id or IRI. Order is preserved (first occurrence wins) so
    the antigen organism, listed first by RCSB, leads the returned list.

    Returns ``[]`` for an EMDB record, a malformed record, or any non-PDB content.
    """
    if not isinstance(content, dict):
        return []
    pdb = content.get("pdb")
    if not isinstance(pdb, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entity in pdb.get("polymer_entities") or []:
        name = entity.get("scientific_name") if isinstance(entity, dict) else None
        if name and isinstance(name, str) and name not in seen:
            seen.add(name)
            out.append(name)
    return out


__all__ = [
    "datacite_title",
    "datacite_description",
    "datacite_subjects",
    "datacite_organisms",
]
