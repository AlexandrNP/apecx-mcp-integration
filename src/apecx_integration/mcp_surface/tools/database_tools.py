"""MCP tools for direct VIOLIN + BV-BRC database queries.

Wraps the vendored pure-pandas query functions in
``apecx_integration.mcp_surface.data.database`` as async MCP tools.
Each tool:

  - lazy-loads the ``DatabaseStore`` on first call (so MCP startup
    isn't blocked when the operator never calls a DB tool);
  - returns ``{"error": "..."}`` instead of raising when data is
    unavailable, so a partial-data deploy still produces useful
    JSON for the model;
  - returns plain dicts (FastMCP serializes them).

These tools sit alongside ``start_workflow`` — the model now has
two paths for database-shaped requests:

  1. Lookup (one-shot, no composition): call ``query_vaccines``,
     ``query_pathogens``, ``query_genes``, ``query_bvbrc_genomes``,
     ``get_vaccine_pathogen_genes``, ``resolve_entity``, or
     ``database_statistics`` directly.
  2. Workflow (composed via the LLM composer): call ``start_workflow``.

For "list vaccines targeting EEEV" the lookup path is correct — it
bypasses the composer entirely and answers in one tool call.

P3.9: each query tool now calls ``lookup_entity()`` on the main
search term before pandas-querying.  On a fast or ancestor path hit
the canonical IRI is extracted and passed as a precision-filter
parameter to the data layer (``ncbi_taxonomy_id``, ``vo_id``,
``ncbi_gene_id``).  On slow/miss the existing substring behaviour is
unchanged.  Results include a ``_resolution`` field when a canonical
match was found so the model sees which path was taken.
"""

from __future__ import annotations

from apecx_integration.mcp_surface.data import database as _db
from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.lookup import LookupResult, lookup_entity

# ---------------------------------------------------------------------------
# IRI → database-ID helpers
# ---------------------------------------------------------------------------


def _ncbi_taxon_id(iri: str | None) -> int | None:
    """Extract integer taxon ID from an NCBITaxon OBO IRI."""
    if iri and "NCBITaxon_" in iri:
        try:
            return int(iri.split("NCBITaxon_")[-1])
        except ValueError:
            pass
    return None


def _vo_local_id(iri: str | None) -> str | None:
    """Extract the local VO identifier (e.g. ``VO_0000001``) from an OBO IRI."""
    if iri and "/obo/VO_" in iri:
        return iri.split("/obo/")[-1]
    return None


def _ncbi_gene_id(iri: str | None) -> int | None:
    """Extract integer gene ID from an identifiers.org ncbigene IRI."""
    if iri and "ncbigene/" in iri:
        try:
            return int(iri.split("ncbigene/")[-1])
        except ValueError:
            pass
    return None


def _resolution_meta(lr: LookupResult) -> dict:
    return {
        "input": lr.surface_form,
        "path": lr.path,
        "canonical_iri": lr.canonical_iri,
        "canonical_label": lr.canonical_label,
        "confidence": lr.confidence,
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


async def query_vaccines(
    search_term: str = "",
    vaccine_type: str = "",
    status: str = "",
    pathogen: str = "",
    limit: int = 25,
) -> dict:
    """Search the VIOLIN vaccine database (~3,500 vaccines).

    Search across vaccine names, types, antigens, and descriptions.
    Filter by vaccine type (e.g. "Subunit vaccine", "Live attenuated
    vaccine"), development status ("Licensed", "Clinical trial",
    "Research"), or target pathogen name. Returns structured vaccine
    records.
    """
    store, err = _db.get_store()
    if store is None:
        return {"error": err or "Database not loaded"}

    vo_id: str | None = None
    resolution: dict | None = None
    if search_term:
        lr = lookup_entity(search_term, entity_type=EntityType.VACCINE)
        if lr.path in ("fast", "ancestor"):
            vo_id = _vo_local_id(lr.canonical_iri)
            resolution = _resolution_meta(lr)

    result = _db.query_vaccines(
        store,
        search_term=search_term or None,
        vaccine_type=vaccine_type or None,
        status=status or None,
        pathogen=pathogen or None,
        limit=limit,
        vo_id=vo_id,
    )
    if resolution:
        result["_resolution"] = resolution
    return result


async def query_pathogens(
    search_term: str = "",
    disease: str = "",
    limit: int = 25,
) -> dict:
    """Search the VIOLIN pathogen database (~220 pathogens).

    Search across pathogen names, diseases, and descriptions.
    Returns pathogen records with disease info, pathogenesis details,
    host immunity information, and associated vaccine counts.
    """
    store, err = _db.get_store()
    if store is None:
        return {"error": err or "Database not loaded"}

    ncbi_id: int | None = None
    resolution: dict | None = None
    if search_term:
        lr = lookup_entity(search_term, entity_type=EntityType.PATHOGEN)
        if lr.path in ("fast", "ancestor"):
            ncbi_id = _ncbi_taxon_id(lr.canonical_iri)
            resolution = _resolution_meta(lr)

    result = _db.query_pathogens(
        store,
        search_term=search_term or None,
        disease=disease or None,
        limit=limit,
        ncbi_taxonomy_id=ncbi_id,
    )
    if resolution:
        result["_resolution"] = resolution
    return result


async def query_genes(
    search_term: str = "",
    organism: str = "",
    limit: int = 25,
) -> dict:
    """Search the VIOLIN gene/protein database (~4,000 genes).

    Search across gene names, protein names, organisms, and molecule
    roles. Returns gene records with NCBI cross-references, PDB IDs,
    and GenBank accessions.
    """
    store, err = _db.get_store()
    if store is None:
        return {"error": err or "Database not loaded"}

    gene_id: int | None = None
    resolution: dict | None = None
    if search_term:
        lr = lookup_entity(search_term, entity_type=EntityType.GENE)
        if lr.path in ("fast", "ancestor"):
            gene_id = _ncbi_gene_id(lr.canonical_iri)
            resolution = _resolution_meta(lr)

    result = _db.query_genes(
        store,
        search_term=search_term or None,
        organism=organism or None,
        limit=limit,
        ncbi_gene_id=gene_id,
    )
    if resolution:
        result["_resolution"] = resolution
    return result


async def query_bvbrc_genomes(
    search_term: str = "",
    species: str = "",
    host: str = "",
    country: str = "",
    min_year: int = 0,
    max_year: int = 0,
    limit: int = 25,
) -> dict:
    """Search the BV-BRC alphavirus genome database (~17,000 genomes).

    Search across genome names, species, strains, and hosts. Filter
    by species, host organism, isolation country, or collection year
    range. Returns genome records with size, GC content, GenBank
    accessions, and sequencing metadata. Pass min_year=0 / max_year=0
    to skip the year filter (avoids the model needing to know about
    sentinel values).
    """
    store, err = _db.get_store()
    if store is None:
        return {"error": err or "Database not loaded"}

    ncbi_id: int | None = None
    resolution: dict | None = None
    if search_term:
        lr = lookup_entity(search_term, entity_type=EntityType.PATHOGEN)
        if lr.path in ("fast", "ancestor"):
            ncbi_id = _ncbi_taxon_id(lr.canonical_iri)
            resolution = _resolution_meta(lr)

    result = _db.query_bvbrc_genomes(
        store,
        search_term=search_term or None,
        species=species or None,
        host=host or None,
        country=country or None,
        min_year=min_year if min_year > 0 else None,
        max_year=max_year if max_year > 0 else None,
        limit=limit,
        ncbi_taxonomy_id=ncbi_id,
    )
    if resolution:
        result["_resolution"] = resolution
    return result


async def get_vaccine_pathogen_genes(pathogen_name: str) -> dict:
    """Get the full vaccine-pathogen-gene relationship chain from VIOLIN.

    For a given pathogen, find all vaccines targeting it and all genes
    associated with those vaccines. Traverses the VIOLIN junction
    tables (vaccine_pathogen, gene_vaccine_pathogen) to build the
    complete relationship graph.
    """
    store, err = _db.get_store()
    if store is None:
        return {"error": err or "Database not loaded"}

    ncbi_id: int | None = None
    resolution: dict | None = None
    if pathogen_name:
        lr = lookup_entity(pathogen_name, entity_type=EntityType.PATHOGEN)
        if lr.path in ("fast", "ancestor"):
            ncbi_id = _ncbi_taxon_id(lr.canonical_iri)
            resolution = _resolution_meta(lr)

    result = _db.get_vaccine_pathogen_genes(store, pathogen_name, ncbi_taxonomy_id=ncbi_id)
    if resolution:
        result["_resolution"] = resolution
    return result


async def resolve_entity(name: str) -> dict:
    """Resolve a biomedical entity name across all APECx databases.

    Searches VIOLIN pathogens, vaccines, genes, and BV-BRC genomes by
    substring match. Returns all matching identifiers (NCBI Taxonomy
    IDs, VIOLIN canonical IDs) so you can use them in targeted
    follow-up queries. Also checks the virus resolution cache for
    pre-resolved mappings. When the synonym dictionary is available,
    includes a ``canonical_resolution`` field with the ontology-level
    canonical IRI and confidence.
    """
    store, err = _db.get_store()
    if store is None:
        return {"error": err or "Database not loaded"}
    result = _db.resolve_entity(store, name)
    lr = lookup_entity(name)
    if lr.path in ("fast", "ancestor"):
        result["canonical_resolution"] = _resolution_meta(lr)
    return result


async def database_statistics() -> dict:
    """Get row counts and column lists for all loaded VIOLIN and BV-BRC tables.

    Call this first to understand what data is available before
    querying. Useful for self-correction when the model is unsure
    whether a column it wants exists.
    """
    store, err = _db.get_store()
    if store is None:
        return {"error": err or "Database not loaded"}
    return _db.database_statistics(store)


__all__ = [
    "database_statistics",
    "get_vaccine_pathogen_genes",
    "query_bvbrc_genomes",
    "query_genes",
    "query_pathogens",
    "query_vaccines",
    "resolve_entity",
]
