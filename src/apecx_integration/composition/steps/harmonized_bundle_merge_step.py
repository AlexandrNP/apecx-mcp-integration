"""HarmonizedBundleMergeStep — fan-in the per-index harmonized search results
into a single bundle for the harmonized ``viral_epitope_analysis`` path.

This is the fan-in step that follows the per-index map over the 9 Globus
DESTINATION indices. The map produces, on the bundle, an ``items`` list — one
``HarmonizedSearchExecuteStep`` result dict per index — plus ``_map_errors``,
the ``resolution_plan`` produced by ``EpitopeResolveStep``, and the original
query/protein passthrough.

Each ``HarmonizedSearchExecuteStep`` result is the envelope shape
``{"envelope_input": {"markdown": str, "data": {"kind": "bundle", "parts":
{...}}}}``. The harmonized/raw record previews live inside ``parts`` —
specifically:

- ``parts.harmonized_query.sample`` — the harmonized (taxon-filtered) record
  preview list (DataCite-projected dicts);
- ``parts.raw_query.sample`` — the raw full-text record preview list;
- ``parts.raw_sample`` — the raw-fallback preview list on a resolution-MISS
  envelope.

(The envelope carries only the projected SAMPLE of each leg, not the full
record list — that is the shape ``HarmonizedSearchExecuteStep`` emits.)

This step flattens those previews across all indices into a single
``globus_results`` list, derives ``taxon_id`` + ``resolved_species_name`` from
the ``resolution_plan``, and records a ``harmonized_search_summary`` (per-index
kept counts + any ``_map_errors``). Everything else on the bundle passes
through unchanged.

Degrade-loud contract: an empty/missing ``items`` produces an empty
``globus_results`` and ``None`` taxon — never a raise. Only a non-dict input is
fatal (``ValueError``, fail-fast on contract — mirrors ``StructuralEvidenceStep``).

Authoring rule alignment (nanobrain-step-authoring skill): ``process()`` only,
``from_config`` only, ``COMPONENT_TYPE`` + ``REQUIRED_CONFIG_FIELDS`` declared.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

_INPUT_KEY = "hmerge_input"


def _records_from_item(item: Any) -> list[dict[str, Any]]:
    """Pull the harmonized + raw record previews out of one per-index
    ``HarmonizedSearchExecuteStep`` result envelope.

    Returns a (possibly empty) list of preview record dicts. A result with no
    records (paused envelope, both legs empty, or a malformed item) yields an
    empty list — degrade-loud, never raise.
    """
    if not isinstance(item, dict):
        return []
    # The result may be the raw envelope dict, or already-unwrapped parts.
    env = item.get("envelope_input")
    if not isinstance(env, dict):
        env = item
    data = env.get("data") if isinstance(env, dict) else None
    parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(parts, dict):
        return []

    records: list[dict[str, Any]] = []
    harm = parts.get("harmonized_query")
    if isinstance(harm, dict) and isinstance(harm.get("sample"), list):
        records.extend(r for r in harm["sample"] if isinstance(r, dict))
    raw = parts.get("raw_query")
    if isinstance(raw, dict) and isinstance(raw.get("sample"), list):
        records.extend(r for r in raw["sample"] if isinstance(r, dict))
    if isinstance(parts.get("raw_sample"), list):
        records.extend(r for r in parts["raw_sample"] if isinstance(r, dict))
    return records


class HarmonizedBundleMergeStepConfig(StepConfig):
    """Config for HarmonizedBundleMergeStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time
    rather than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Framework tracking attribute set by ConfigBase.from_config after
    # construction. Declared so extra="forbid" doesn't block setattr.
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class HarmonizedBundleMergeStep(BaseStep):
    COMPONENT_TYPE: str = "harmonized_bundle_merge_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return HarmonizedBundleMergeStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"HarmonizedBundleMergeStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap the framework trigger envelope ({input_du: bundle}); direct
        # callers (tests) pass the bundle raw.
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        bundle = dict(input_data)  # shallow copy; we add the merged fields

        from apecx_integration.composition.steps.harmonized_search_execute_step import (
            _iri_to_taxon_id,
        )

        items = bundle.get("items")
        if not isinstance(items, list):
            items = []
        index_names = bundle.get("index_names")
        if not isinstance(index_names, list):
            index_names = []
        map_errors = bundle.get("_map_errors")

        globus_results: list[dict[str, Any]] = []
        per_index_counts: dict[str, int] = {}
        for i, item in enumerate(items):
            recs = _records_from_item(item)
            label = index_names[i] if i < len(index_names) else f"index_{i}"
            per_index_counts[str(label)] = len(recs)
            globus_results.extend(recs)

        # Derive taxon_id + resolved species name from the resolution plan.
        plan = bundle.get("resolution_plan")
        taxon_id: int | None = None
        resolved_species_name: str | None = None
        if isinstance(plan, dict):
            iri = plan.get("canonical_iri")
            if isinstance(iri, str) and iri:
                taxon_id = _iri_to_taxon_id(iri)
            label = plan.get("canonical_label")
            if isinstance(label, str) and label:
                resolved_species_name = label

        bundle["globus_results"] = globus_results
        bundle["taxon_id"] = taxon_id
        bundle["resolved_species_name"] = resolved_species_name
        bundle["harmonized_search_summary"] = {
            "per_index_kept": per_index_counts,
            "total_records": len(globus_results),
            "map_errors": map_errors,
        }

        log.info(
            "HarmonizedBundleMergeStep %s: indices=%d total_records=%d taxon_id=%s map_errors=%s",
            self.name,
            len(items),
            len(globus_results),
            taxon_id,
            "set" if map_errors else "none",
        )
        return bundle
