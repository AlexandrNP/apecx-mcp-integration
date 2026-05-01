"""Adapter that wraps an :class:`EntityResolutionTransform` for use as an
``apecx_harvesters.pipeline.run.Transform`` (i.e. ``Callable[[DataCite],
Awaitable[DataCite]]``).

Why this is needed
==================

Per the v5 plan (``ontology_integration_initial_analysis.md`` §0.2),
the synonym-dictionary work is being developed local-first.  At Phase 6
the same transform must plug into the apecx-harvesters pipeline.  This
adapter is what makes that lift wrapper-only rather than a refactor.

Why this is harder than it looks
================================

DataCite uses Pydantic ``extra='forbid'`` (good — same workspace rule
we follow).  That means our ``EntityResolutionTransform`` cannot simply
add ``canonical_iri`` / ``canonical_label`` / etc. as extra fields on a
base DataCite — Pydantic will reject the unknown keys at re-validation.

The harvester's design pattern (``apecx-harvesters/CLAUDE.md``) addresses
exactly this case: domain-specific extension fields go on a DataCite
subclass.  Quoting the CLAUDE.md: *"Each API-specific dataclass should
be a subclass of the main `Datacite` schema. Domain-specific information
should be encoded in a nested field. For example, the PDB harvester
might generate `class PDBContainer(DataCite): pdb: PDBFields`."*

So the adapter MUST be parameterized by which extension field on the
DataCite subclass it writes the resolution result into.  At Phase 6,
the harvester supplies a DataCite subclass with a known extension field
(probably ``canonical: CanonicalExtension``).  At Phase 1 / contract
testing, the same shape is exercised against a local sample subclass.

The contract test surfaces this constraint *now* rather than at Phase 6
integration time — which was the entire point of writing it.

Phase 6 caveats
===============

1. The adapter does NOT import ``apecx_harvesters`` at module load.
2. A Phase 6 ADR in ``apecx-harvesters/design/`` should ratify the
   shape of the extension field (whether ``canonical: CanonicalExtension``
   or some other contract).  Until that ADR lands, the
   ``extension_field`` parameter is the integration knob.
3. If the eventual harvester pattern places resolution data in
   DataCite's existing ``alternateIdentifiers`` slot rather than in
   a custom extension field, the adapter needs a different strategy
   class.  The current implementation does not handle that path —
   add it when (and only when) the harvester ADR commits to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from apecx_integration.synonym_dictionary.transform import (
    RESOLUTION_OUTPUT_KEYS,
    EntityRecord,
    EntityResolutionTransform,
)

if TYPE_CHECKING:
    # Imported only for static type-checking.  Runtime code uses duck typing
    # via the .model_dump() / .model_validate() Pydantic protocol so this
    # adapter remains importable when apecx_harvesters isn't on the path.
    from apecx_harvesters.loaders.base import DataCite


D = TypeVar("D", bound="DataCite")


def adapt_to_harvester_transform(
    entity_transform: EntityResolutionTransform,
    *,
    extension_field: str,
) -> Any:
    """Wrap an ``EntityResolutionTransform`` for use as an apecx-harvesters
    ``Transform`` (``Callable[[DataCite], Awaitable[DataCite]]``).

    Parameters
    ----------
    entity_transform:
        The dict-shaped transform produced by Stage 1.
    extension_field:
        Name of a field on the input record's concrete DataCite subclass
        where the resolution result will be packed.  The field should be
        a Pydantic model (or dict-shaped) with at least the
        :data:`RESOLUTION_OUTPUT_KEYS` keys.

        Required because base ``DataCite`` uses ``extra='forbid'`` and
        cannot absorb the resolution keys directly — extending DataCite
        via subclassing is the documented harvester pattern.

    Returns
    -------
    A callable matching the harvester's ``Transform`` signature.

    Behavior
    --------
    For each incoming DataCite record:

    1. ``record.model_dump()`` -> dict.
    2. ``entity_transform(dict)`` -> dict with resolution keys added.
    3. The resolution keys are extracted into a sub-dict and assigned to
       ``record_dict[extension_field]``.
    4. ``type(record).model_validate(record_dict)`` to re-construct the
       same concrete DataCite-subclass type.
    """

    async def adapted(record: D) -> D:
        # Pydantic protocol — DataCite is a BaseModel so model_dump exists.
        as_dict: EntityRecord = record.model_dump()  # type: ignore[attr-defined]
        enriched = await entity_transform(as_dict)
        # Extract the resolution-output keys; leave the rest of the
        # DataCite-shaped payload alone.
        resolution_payload: dict[str, Any] = {}
        cleaned: EntityRecord = {}
        for key, value in enriched.items():
            if key in RESOLUTION_OUTPUT_KEYS:
                resolution_payload[key] = value
            else:
                cleaned[key] = value
        cleaned[extension_field] = resolution_payload
        # Re-validate using the input's concrete type so DataCite subclasses
        # round-trip cleanly.
        return type(record).model_validate(cleaned)  # type: ignore[return-value]

    return adapted
