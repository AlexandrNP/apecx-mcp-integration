"""Enumerations for the synonym dictionary.

All values are stable strings — they appear in the on-disk artifact
(SQLite columns, manifest JSON) and in the Stage 2 lookup API.  Changing
a string here is a contract-breaking change requiring a schema-version
bump.
"""

from __future__ import annotations

from enum import StrEnum


class EntityType(StrEnum):
    """The kind of entity a dictionary entry refers to.

    Drawn from the entity-type buckets used by ``apecx_db_integration``'s
    ``extract_entities_llm`` and ``get_candidate_terms``.
    """

    PATHOGEN = "pathogen"
    VACCINE = "vaccine"
    DISEASE = "disease"
    GENE = "gene"
    GENOME = "genome"  # BV-BRC genome rows


class ResolutionStatus(StrEnum):
    """Provenance tag describing how a row's canonical IRI was determined.

    The taxonomy is load-bearing for the §4.10 unresolved-row policy: rows
    with status ``UNRESOLVED`` are surfaced explicitly to the user as
    "did not join" rather than being hidden.  See
    ``_workspace_notes/apecx-mcp-integration_dev_history/scope_decisions/09_unresolved_row_policy.md``.
    """

    # Source row already carried an authoritative ID (e.g. NCBI_Taxonomy_ID,
    # Vaccine_Ontology_ID).  Synonyms fetched from OLS for that IRI.
    # Highest confidence: 1.0.
    ID_ANCHORED = "id_anchored"

    # OLS exact-match resolution against a per-ontology search.  The
    # source-row label was found verbatim (case-insensitive) in OLS as
    # a label or synonym of a single concept.  Confidence ~0.9.
    OLS_EXACT = "ols_exact"

    # OLS fuzzy / multi-match resolution.  Either the search returned
    # multiple candidates and we picked one by row-context disambiguation,
    # or the match was non-exact.  Confidence < 0.9.
    OLS_FUZZY = "ols_fuzzy"

    # Project-private IRI in the ``apecx_local:`` namespace.  Used for
    # entities that are real but external-ontology-absent (lab strains,
    # project-specific vaccine candidates, internally-curated registries).
    # Per §4.9 (3) of the analysis doc.
    PROJECT_LOCAL = "project_local"

    # No resolution.  Source row stays in the dictionary with
    # ``canonical_iri = None`` so it surfaces in the unresolved tier.
    # Per §4.10 of the analysis doc.
    UNRESOLVED = "unresolved"


class OntologyName(StrEnum):
    """Authoritative-source identifier; pinned per dictionary build via the
    ``BuildManifest.ontology_versions`` field."""

    NCBITAXON = "ncbitaxon"
    VO = "vo"
    DOID = "doid"
    GO = "go"
    APECX_LOCAL = "apecx_local"
