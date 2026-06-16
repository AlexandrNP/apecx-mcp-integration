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


# Citation-token precedence: the most object-specific identifier first. NCBI-Taxonomy is
# deliberately excluded — it names the organism, not the record, so it can't cite a specific
# object (every record for a virus shares it).
_PRIMARY_ID_PRECEDENCE: tuple[str, ...] = (
    "PDB",
    "EMDB",
    "GenBank",
    "RefSeq",
    "BVBRC-Genome",
    "BVBRC-Protein",
    "UniProt",
    "DOI",
)


def datacite_identifiers(content: Any) -> dict[str, list[str]]:
    """Return the object identifiers of a Globus record, grouped by type.

    Pulls ``alternateIdentifiers[]`` ({alternateIdentifier, alternateIdentifierType} —
    GenBank / PDB / UniProt / BVBRC-Genome / NCBI-Taxonomy / …) and DOI-typed
    ``relatedIdentifiers[]``. Values are split on ``;`` (BV-BRC doubles UniProt as
    ``"Q1H8W5;Q1H8W5"``) and deduped per type, order preserved. These are the concrete
    references a reader needs to trace a claim back to a specific database object — the
    fields the old projection silently dropped.
    """
    out: dict[str, list[str]] = {}
    if not isinstance(content, dict):
        return out

    def _add(id_type: Any, raw: Any) -> None:
        if not id_type or not raw:
            return
        bucket = out.setdefault(str(id_type), [])
        for piece in str(raw).split(";"):
            v = piece.strip()
            if v and v not in bucket:
                bucket.append(v)

    for entry in content.get("alternateIdentifiers") or []:
        if isinstance(entry, dict):
            _add(entry.get("alternateIdentifierType"), entry.get("alternateIdentifier"))
    for entry in content.get("relatedIdentifiers") or []:
        if isinstance(entry, dict) and entry.get("relatedIdentifierType") == "DOI":
            _add("DOI", entry.get("relatedIdentifier"))
    return out


def datacite_primary_id(content: Any) -> str | None:
    """Return the best single citation token for a record (e.g. ``"PDB:7H6J"``,
    ``"GenBank:LT964945"``), or ``None`` if it carries no object-specific identifier.

    The token is a clean ``Type:Value`` (no whitespace/brackets) so it survives the
    inline-citation regex and can be used as the record's ``subject`` at render. Precedence
    favors the most specific object id; NCBI-Taxonomy is never primary (it's the organism).
    """
    ids = datacite_identifiers(content)
    for id_type in _PRIMARY_ID_PRECEDENCE:
        vals = ids.get(id_type)
        if vals:
            return f"{id_type}:{vals[0]}"
    return None


def datacite_taxon_iris(content: Any) -> list[str]:
    """Return the DataCite ``subjects[].valueUri`` taxon IRIs (deduped, order preserved)."""
    if not isinstance(content, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in content.get("subjects") or []:
        uri = entry.get("valueUri") if isinstance(entry, dict) else None
        if uri and isinstance(uri, str) and uri not in seen:
            seen.add(uri)
            out.append(uri)
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
    "datacite_identifiers",
    "datacite_primary_id",
    "datacite_taxon_iris",
    "datacite_organisms",
]
