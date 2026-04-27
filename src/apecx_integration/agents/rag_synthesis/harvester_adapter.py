"""Bridge from apecx-harvesters ``DataCite`` records to the flat
publication-dict shape ``synthesize_response`` expects.

The harvester emits rich nested DataCite records (Identifier, Title,
Creator, Publisher, Description); the synthesizer's
``_render_publications`` consumes a flat dict
(``{doi, title, authors, year, journal, abstract}``). This module is
the single place that knows both shapes.

Probe 916 (boundary invariant): every reference to "DataCite" in
``apecx_integration`` lives under ``agents/rag_synthesis/`` — that
makes this module the right home, not (e.g.) ``db_integration`` or
``control_plane``. If a future caller wants the bridge from elsewhere,
they import it from here; they do not re-import ``DataCite`` themselves.
"""

from __future__ import annotations

from typing import Any

from apecx_harvesters.loaders.base.model import DataCite, DescriptionType


def datacite_to_publication(record: DataCite) -> dict[str, Any]:
    """Convert a DataCite record to a synthesizer publication dict.

    Args:
        record: A DataCite-shaped harvester output. The ``identifier``
            must carry a DOI (``identifierType == "DOI"``); records
            without a DOI are not citable by the synthesizer (the
            ``[10.x/...]`` regex requires a DOI literal).

    Returns:
        A flat dict matching the ``_render_publications`` contract:
        ``{doi, title, authors, year, journal, abstract}``. Optional
        fields are omitted (not nulled) so the renderer's
        ``or "(unspecified)"`` fallbacks fire correctly.

    Raises:
        ValueError: ``record.identifier`` is None or not a DOI. This
            is fail-fast — the synthesizer would later reject a
            publication without a DOI anyway, and surfacing the
            failure here gives a clearer error path.
    """
    if record.identifier is None:
        raise ValueError(
            "datacite_to_publication: record has no identifier; the "
            "synthesizer's publication renderer requires a DOI literal "
            "as the citation token. Source records without an "
            "identifier are not consumable by this pipeline."
        )
    if record.identifier.identifierType != "DOI":
        raise ValueError(
            f"datacite_to_publication: identifierType is "
            f"{record.identifier.identifierType!r}, not 'DOI'. The "
            f"synthesizer's citation pattern requires DOI literals "
            f"(``10.<id>/...``); other identifier types cannot be "
            f"cited inline."
        )

    pub: dict[str, Any] = {"doi": record.identifier.identifier}

    if record.titles:
        # Prefer the primary title (titleType is None) when present;
        # fall back to the first title.
        primary = next(
            (t for t in record.titles if t.titleType is None),
            record.titles[0],
        )
        pub["title"] = primary.title

    authors: list[str] = []
    for c in record.creators:
        if c.givenName and c.familyName:
            authors.append(f"{c.givenName} {c.familyName}")
        elif c.name:
            authors.append(c.name)
    if authors:
        pub["authors"] = authors

    if record.publicationYear:
        pub["year"] = record.publicationYear

    if record.publisher and record.publisher.name:
        pub["journal"] = record.publisher.name

    # First Abstract description wins. (DataCite allows multiple
    # descriptions of different types; the synthesizer renders one
    # blurb per publication.)
    for d in record.descriptions:
        if d.descriptionType == DescriptionType.Abstract:
            pub["abstract"] = d.description
            break

    return pub
