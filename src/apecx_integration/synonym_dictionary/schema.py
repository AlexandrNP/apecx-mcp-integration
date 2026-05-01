"""Pydantic schemas for the synonym dictionary artifact.

These models define the **on-disk and over-wire contract** between Stage 1
(builder) and Stage 2 (runtime).  Every model sets ``extra='forbid'`` per
the workspace-wide rule (memory: ``pydantic_extra_forbid_rule.md``):
a typo in YAML or JSON that would silently use a default is preferable
to fail loudly at parse time.

Schema versioning:

- The literal version string is on :class:`BuildManifest.schema_version`.
- Bumping it is a coordinated cross-repo change.  Stage 2 readers must
  refuse to load incompatible versions.
- Major bump = breaking change to any model field's type or required-ness.
- Minor bump = adding optional fields with defaults.

Confidence semantics (numeric, ``0.0 <= confidence <= 1.0``):

- ``1.0`` — :attr:`ResolutionStatus.ID_ANCHORED` (source had authoritative ID).
- ``0.9`` — :attr:`ResolutionStatus.OLS_EXACT` (exact-string OLS match).
- ``0.5 - 0.89`` — :attr:`ResolutionStatus.OLS_FUZZY` (fuzzy OLS match;
  exact value depends on the OLS score).
- ``1.0`` — :attr:`ResolutionStatus.PROJECT_LOCAL` (synthetic IRI; we
  control its uniqueness so it's locally authoritative).
- ``0.0`` — :attr:`ResolutionStatus.UNRESOLVED` (no resolution; the
  numeric is unused but pinned to 0.0 for ordering/filtering).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from apecx_integration.synonym_dictionary.enums import (
    EntityType,
    OntologyName,
    ResolutionStatus,
)

_FROZEN_FORBID = ConfigDict(extra="forbid", frozen=True)


class ResolutionResult(BaseModel):
    """Per-record output of a :class:`EntityResolutionTransform`.

    Stage 2 callers reading enriched rows reconstruct this from the
    new columns added to harvested CSVs (``canonical_iri``,
    ``canonical_label``, ``resolution_status``, etc.).
    """

    model_config = _FROZEN_FORBID

    canonical_iri: str | None = Field(
        description=(
            "OBO IRI (e.g. 'http://purl.obolibrary.org/obo/NCBITaxon_37124') "
            "or apecx-local IRI ('apecx_local:vaccine_candidate/<hash>'). "
            "None when status == UNRESOLVED."
        ),
    )
    canonical_label: str | None = Field(
        description=(
            "Display label.  See open question §5(4) of the analysis doc — "
            "this is the OLS preferred label by default until a UX decision "
            "is made.  None when status == UNRESOLVED."
        ),
    )
    canonical_ontology: OntologyName | None = Field(
        description=("Disambiguates the IRI namespace.  None when status == UNRESOLVED."),
    )
    synonyms: tuple[str, ...] = Field(
        default=(),
        description=(
            "All known surface forms for this IRI, fetched from OLS at build "
            "time.  Stage 2's lookup API uses these to map free-text user "
            "queries to the IRI."
        ),
    )
    resolution_status: ResolutionStatus
    resolution_confidence: float = Field(ge=0.0, le=1.0)
    dictionary_version: str = Field(
        description=(
            "Identifier of the build that produced this row, e.g. "
            "'2026-05-01.1'.  Pins the row to a specific OLS snapshot for "
            "auditability and re-resolution decisions."
        ),
    )


class DictionaryEntry(BaseModel):
    """One entry per ``(entity_type, canonical_iri)`` tuple in the dictionary.

    Aggregates surface forms encountered across ALL harvested databases that
    refer to the same canonical IRI — this is what Stage 2's free-text
    lookup hits.
    """

    model_config = _FROZEN_FORBID

    entity_type: EntityType
    canonical_iri: str
    canonical_label: str
    synonyms: tuple[str, ...] = ()
    ontology: OntologyName
    ontology_version: str = Field(
        description=(
            "Pinned ontology release (e.g. 'ncbitaxon-2026-04-01') — must "
            "match a key in the BuildManifest.ontology_versions dict."
        ),
    )
    source_records: tuple[str, ...] = Field(
        default=(),
        description=(
            "Origin records that contributed surface forms or anchored to "
            "this IRI.  Format: '<database>.<table>.<row_id>', e.g. "
            "'violin.pathogen.42'.  Used for provenance + re-resolution "
            "audit."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)
    resolved_at: datetime


class ProvisionalSynonym(BaseModel):
    """User-supplied synonym candidate, not yet promoted into the dictionary.

    Phase 4 of the v5 plan is **deferred** (see analysis doc §6.1) — this
    schema is locked now per task P1.7 so the on-disk shape doesn't change
    when Phase 4 finally lands.
    """

    model_config = _FROZEN_FORBID

    entity_type: EntityType
    canonical_iri: str | None = Field(
        description=(
            "The IRI the user accepted/proposed for this surface form.  "
            "None means 'user reports this surface form has no good match' "
            "— a separate signal worth recording."
        ),
    )
    surface_form: str = Field(
        description="The user-typed term.",
    )
    proposed_at: datetime
    proposed_by: str = Field(
        description=(
            "User identifier (or 'anonymous') for moderation/abuse-tracking. "
            "Per analysis doc §4.7, the provisional list is a moderation "
            "system in disguise; provenance matters."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)
    promotion_signals: tuple[str, ...] = Field(
        default=(),
        description=(
            "Voter IDs / decision events that argue for promoting this "
            "provisional into the main dictionary.  Schema-stable for "
            "Phase 4 readiness."
        ),
    )


class BuildManifest(BaseModel):
    """Metadata accompanying a dictionary artifact.

    Without this, downstream consumers cannot reason about what they're
    reading: which OLS version, which harvester revision, how many records
    failed to resolve, etc.  Per analysis doc §3.4 (3).
    """

    model_config = _FROZEN_FORBID

    schema_version: str = Field(
        default="1.0.0",
        description=(
            "Dictionary-artifact schema version.  Stage 2 readers refuse "
            "incompatible major versions."
        ),
    )
    dictionary_version: str = Field(
        description=("Build identifier, e.g. '2026-05-01.1' — unique per build."),
    )
    built_at: datetime
    harvester_version: str | None = Field(
        default=None,
        description=(
            "Populated when this dictionary is produced via apecx-harvesters "
            "(Phase 6+).  None for local-CLI builds in Phases 1-5."
        ),
    )
    ontology_versions: dict[str, str] = Field(
        description=(
            "Ontology name -> version pin (e.g. {'ncbitaxon': '2026-04-01'}). "
            "Keys are the StrEnum string values of OntologyName."
        ),
    )
    record_counts_per_entity_type: dict[EntityType, int]
    unresolved_count: int = Field(
        ge=0,
        description=(
            "Total rows with resolution_status == UNRESOLVED.  Per §4.10 "
            "of the analysis doc, these surface explicitly in the Stage 2 "
            "result; their count is a quality-of-build signal."
        ),
    )
    record_count_total: int = Field(ge=0)
