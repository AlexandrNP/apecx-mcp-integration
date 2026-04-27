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
"""

from __future__ import annotations

from apecx_integration.mcp_surface.data import database as _db


def _store_or_error() -> tuple[_db.DatabaseStore | None, dict | None]:
    """Return ``(store, None)`` if loadable, else ``(None, error_dict)``."""
    store, err = _db.get_store()
    if store is None:
        return None, {"error": err or "Database not loaded"}
    return store, None


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
    store, error = _store_or_error()
    if error is not None:
        return error
    return _db.query_vaccines(
        store,
        search_term=search_term or None,
        vaccine_type=vaccine_type or None,
        status=status or None,
        pathogen=pathogen or None,
        limit=limit,
    )


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
    store, error = _store_or_error()
    if error is not None:
        return error
    return _db.query_pathogens(
        store,
        search_term=search_term or None,
        disease=disease or None,
        limit=limit,
    )


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
    store, error = _store_or_error()
    if error is not None:
        return error
    return _db.query_genes(
        store,
        search_term=search_term or None,
        organism=organism or None,
        limit=limit,
    )


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
    store, error = _store_or_error()
    if error is not None:
        return error
    return _db.query_bvbrc_genomes(
        store,
        search_term=search_term or None,
        species=species or None,
        host=host or None,
        country=country or None,
        min_year=min_year if min_year > 0 else None,
        max_year=max_year if max_year > 0 else None,
        limit=limit,
    )


async def get_vaccine_pathogen_genes(pathogen_name: str) -> dict:
    """Get the full vaccine-pathogen-gene relationship chain from VIOLIN.

    For a given pathogen, find all vaccines targeting it and all genes
    associated with those vaccines. Traverses the VIOLIN junction
    tables (vaccine_pathogen, gene_vaccine_pathogen) to build the
    complete relationship graph.
    """
    store, error = _store_or_error()
    if error is not None:
        return error
    return _db.get_vaccine_pathogen_genes(store, pathogen_name)


async def resolve_entity(name: str) -> dict:
    """Resolve a biomedical entity name across all APECx databases.

    Searches VIOLIN pathogens, vaccines, genes, and BV-BRC genomes by
    substring match. Returns all matching identifiers (NCBI Taxonomy
    IDs, VIOLIN canonical IDs) so you can use them in targeted
    follow-up queries. Also checks the virus resolution cache for
    pre-resolved mappings.
    """
    store, error = _store_or_error()
    if error is not None:
        return error
    return _db.resolve_entity(store, name)


async def database_statistics() -> dict:
    """Get row counts and column lists for all loaded VIOLIN and BV-BRC tables.

    Call this first to understand what data is available before
    querying. Useful for self-correction when the model is unsure
    whether a column it wants exists.
    """
    store, error = _store_or_error()
    if error is not None:
        return error
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
