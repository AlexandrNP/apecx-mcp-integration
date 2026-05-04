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
    from nanobrain.core.workflow import Workflow


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


def adapt_workflow_to_harvester_transform(
    workflow: Workflow,
    *,
    extension_field: str,
    entity_type_field: str = "entity_type",
    surface_form_field: str = "surface_form",
) -> Any:
    """Wrap a nanobrain :class:`Workflow` as an apecx-harvesters ``Transform``.

    Companion to :func:`adapt_to_harvester_transform`, but for the
    nanobrain-wrapped pipeline (``iri_resolution_workflow.yml``) instead of
    the plain-Python :class:`EntityResolutionTransform`. Use this when the
    harvester pipeline is consuming the workflow form so the same multi-step
    DAG can be exercised both standalone and as a harvester transform.

    Parameters
    ----------
    workflow:
        A :class:`nanobrain.core.workflow.Workflow` instance constructed via
        ``Workflow.from_config('iri_resolution_workflow.yml')`` (or any
        workflow with the same input/output port shape: an ``entity_records``
        input data unit accepting ``{"entity_records": [...]}`` and a
        ``resolved_records`` output data unit emitting
        ``{"resolved_records": [...]}``).
    extension_field:
        Name of a field on the input record's concrete DataCite subclass
        where the resolution result will be packed. Same semantics as
        :func:`adapt_to_harvester_transform` — required because base
        DataCite uses ``extra='forbid'``.
    entity_type_field:
        Name of the field on the DataCite payload that carries the
        ``EntityType`` string ("pathogen", "vaccine", etc.). Defaults to
        ``"entity_type"``. The value flows through the workflow as-is
        (the resolve step does the string→enum coercion).
    surface_form_field:
        Name of the field on the DataCite payload that carries the
        user-typed surface form. Defaults to ``"surface_form"``.

    Returns
    -------
    A callable matching the harvester's ``Transform`` signature.

    Behavior
    --------
    For each incoming DataCite record:

    1. ``record.model_dump()`` -> dict.
    2. The ``surface_form_field`` and ``entity_type_field`` values are
       packaged into a single-record batch and deposited into the
       workflow's ``entity_records`` input data unit.
    3. ``await workflow.execute()`` runs the DAG (normalize → resolve).
    4. The first (and only) item from ``resolved_records`` is read back.
    5. The resolution-output keys (per :data:`RESOLUTION_OUTPUT_KEYS`) are
       written into ``record_dict[extension_field]``; everything else
       (including ``resolution_path`` and ``_original_surface_form``) is
       dropped before re-validation, since the DataCite subclass doesn't
       declare them.
    6. ``type(record).model_validate(record_dict)`` re-constructs the
       same concrete DataCite-subclass type.

    Lifecycle note
    --------------
    The workflow instance is held by reference; do NOT shut it down
    while this transform is in use. Construct one workflow per
    long-running harvester pipeline; tear it down on pipeline stop.
    """

    async def adapted(record: D) -> D:
        as_dict: EntityRecord = record.model_dump()  # type: ignore[attr-defined]
        surface_form = as_dict.get(surface_form_field)
        entity_type = as_dict.get(entity_type_field)

        # Drive the workflow directly via its ``process()`` method.
        # ``workflow.execute()`` would route through nanobrain's data-driven
        # cascade, which has known issues with inter-step data propagation
        # (see ``IRIResolutionWorkflow`` docstring). ``process()`` is the
        # imperative entry point on the custom Workflow subclass and is
        # the canonical interface for in-process invocation.
        workflow_input = {
            "entity_records": [
                {
                    surface_form_field: surface_form,
                    entity_type_field: entity_type,
                }
            ]
        }
        workflow_output = await workflow.process(workflow_input)
        resolved_records = workflow_output.get("resolved_records") or []

        if not resolved_records:
            # The workflow ran but produced nothing — surface as a miss-shaped
            # payload so downstream re-validation against DataCite still
            # succeeds with empty resolution fields.
            resolution_payload: dict[str, Any] = {key: None for key in RESOLUTION_OUTPUT_KEYS}
            resolution_payload["resolution_status"] = None
            resolution_payload["resolution_confidence"] = 0.0
        else:
            resolved = resolved_records[0]
            resolution_payload = {key: resolved.get(key) for key in RESOLUTION_OUTPUT_KEYS}

        # Drop the resolution-output keys + workflow-internal fields from
        # the cleaned DataCite-shaped payload; the DataCite subclass
        # doesn't declare them and ``extra='forbid'`` would otherwise reject.
        cleaned: EntityRecord = {}
        for key, value in as_dict.items():
            if key in RESOLUTION_OUTPUT_KEYS:
                continue
            if key in {"resolution_path", "_original_surface_form"}:
                continue
            cleaned[key] = value
        cleaned[extension_field] = resolution_payload
        return type(record).model_validate(cleaned)  # type: ignore[return-value]

    return adapted
