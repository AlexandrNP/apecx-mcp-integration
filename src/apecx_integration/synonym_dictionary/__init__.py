"""Synonym dictionary — local-first build of canonical-IRI resolution.

Provides the build-time + runtime infrastructure for harmonizing entities
across the project's databases (VIOLIN, BV-BRC, future harvesters) under
shared canonical identifiers from authoritative external ontologies (NCBI
Taxonomy, Vaccine Ontology, Disease Ontology, Gene Ontology).

See ``_workspace_notes/apecx-mcp-integration_dev_history/ontology_integration_initial_analysis.md``
(v5) for the full architecture rationale.

This is the **Phase 1 contract scaffold** per
``ontology_integration_task_plan.md``.  Phase 2 (Stage 1 MVP) will land
the OLS client, resolvers, and CLI on top of this foundation.

Public API exports:

- :class:`EntityType`, :class:`ResolutionStatus`, :class:`OntologyName`
- :class:`ResolutionResult`, :class:`DictionaryEntry`, :class:`BuildManifest`,
  :class:`ProvisionalSynonym`
- :class:`DictionaryWriter`, :class:`DictionaryReader` — abstract IO contracts
- ``EntityRecord``, ``EntityResolutionTransform`` — runtime transform contract
"""

from apecx_integration.synonym_dictionary.enums import (
    EntityType,
    OntologyName,
    ResolutionStatus,
)
from apecx_integration.synonym_dictionary.io import (
    DictionaryReader,
    DictionaryWriter,
)
from apecx_integration.synonym_dictionary.schema import (
    BuildManifest,
    DictionaryEntry,
    ProvisionalSynonym,
    ResolutionResult,
)
from apecx_integration.synonym_dictionary.transform import (
    EntityRecord,
    EntityResolutionTransform,
)

__all__ = [
    "BuildManifest",
    "DictionaryEntry",
    "DictionaryReader",
    "DictionaryWriter",
    "EntityRecord",
    "EntityResolutionTransform",
    "EntityType",
    "OntologyName",
    "ProvisionalSynonym",
    "ResolutionResult",
    "ResolutionStatus",
]
