"""Entity-resolution transform contract.

Defines the shape of the per-record transformation that Stage 1 applies
during a dictionary build, and that the harvester adapter wraps at Phase 6.

Why a generic ``EntityRecord`` rather than ``apecx_harvesters.DataCite``?
========================================================================

The harvester's ``Transform = Callable[[DataCite], Awaitable[DataCite]]``
type targets bibliographic records (DataCite has fields like ``creators``,
``titles``, ``publisher``).  VIOLIN/BV-BRC entity tables have fields like
``Pathogen``, ``NCBI_Taxonomy_ID`` — a different semantic.  A literal
match against ``Transform[DataCite]`` would coerce VIOLIN rows into a
DataCite-shaped representation, which is awkward.

Instead, this module defines an :class:`EntityRecord` (free-form dict)
plus :class:`EntityResolutionTransform`, and the
:mod:`apecx_integration.synonym_dictionary.harvester_adapter` module
provides a wrapper that adapts ``DataCite`` (or a DataCite subclass for
entity-table harvesters) to ``EntityRecord`` and back.  At Phase 6, the
adapter is what plugs into the harvester pipeline; the underlying
:class:`EntityResolutionTransform` is portable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# Loose-typed entity record.  Keys are column / field names from the
# underlying database (e.g. 'Pathogen', 'NCBI_Taxonomy_ID', 'Vaccine_Name').
# Stage 1's transform reads from these and writes the canonical_* keys
# defined by ResolutionResult.
#
# Locking this as ``dict[str, Any]`` rather than a Pydantic model keeps
# the transform usable against the heterogeneous CSV/TSV shapes already
# in ``data/`` without per-table modelling.  The structured shape is the
# OUTPUT (ResolutionResult), not the INPUT.
EntityRecord = dict[str, Any]


# Async transform: takes one entity record, returns the same record with
# canonical-name fields added.  Matches the SHAPE (one in, same shape out,
# async) of apecx_harvesters' Transform[DataCite] without committing to
# DataCite as the carrier type.
EntityResolutionTransform = Callable[[EntityRecord], Awaitable[EntityRecord]]


# The output keys that an EntityResolutionTransform contributes.  Listed
# here so the integration adapter, the SQLite writer, and the CLI all
# agree on what's added.  Renaming any of these is a breaking change to
# the dictionary artifact shape.
RESOLUTION_OUTPUT_KEYS: tuple[str, ...] = (
    "canonical_iri",
    "canonical_label",
    "canonical_ontology",
    "resolution_status",
    "resolution_confidence",
    "dictionary_version",
)
