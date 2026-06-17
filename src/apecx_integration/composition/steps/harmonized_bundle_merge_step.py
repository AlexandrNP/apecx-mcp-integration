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
    """Pull the full record set out of one per-index ``HarmonizedSearchExecuteStep``
    result envelope.

    The harmonized leg is the canonical taxon-filtered set; the raw leg is the
    noisier full-text superset. The two overlap heavily (for chikungunya: raw 6687,
    harmonized 6684 — nearly identical), so we take ONE leg, never both — concatenating
    would double-carry the entire corpus. Preference: the harmonized leg when it carried
    records; the raw leg as a fallback when harmonization returned nothing (broken /
    zero-harmonization dict-staleness case, where raw is the trustworthy signal).

    Returns a (possibly empty) list of record dicts. A result with no records (paused
    envelope, both legs empty, or a malformed item) yields an empty list — degrade-loud,
    never raise.
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

    # Preferred path: the FULL ``records`` lists the search step now carries.
    harm = parts.get("harmonized_query")
    if isinstance(harm, dict) and isinstance(harm.get("records"), list) and harm["records"]:
        return [r for r in harm["records"] if isinstance(r, dict)]
    raw = parts.get("raw_query")
    if isinstance(raw, dict) and isinstance(raw.get("records"), list) and raw["records"]:
        return [r for r in raw["records"] if isinstance(r, dict)]
    # Resolution-MISS envelope carries the full raw fallback under ``raw_records``.
    if isinstance(parts.get("raw_records"), list) and parts["raw_records"]:
        return [r for r in parts["raw_records"] if isinstance(r, dict)]

    # Legacy fallback: envelopes WITHOUT full record lists (old cached envelopes,
    # unit fixtures) — concatenate the small samples so they still contribute.
    records: list[dict[str, Any]] = []
    if isinstance(harm, dict) and isinstance(harm.get("sample"), list):
        records.extend(r for r in harm["sample"] if isinstance(r, dict))
    if isinstance(raw, dict) and isinstance(raw.get("sample"), list):
        records.extend(r for r in raw["sample"] if isinstance(r, dict))
    if isinstance(parts.get("raw_sample"), list):
        records.extend(r for r in parts["raw_sample"] if isinstance(r, dict))
    return records


def _available_from_item(item: Any) -> int:
    """Total records AVAILABLE in the index for this query (distinct from KEPT/retrieved).

    MUST report the total of the SAME leg ``_records_from_item`` took for "used" — otherwise a
    dict-staleness case (harmonized empty, raw populated) would render "0 available / N used".
    So: the harmonized leg's total when it supplied records; else the raw leg's total when it
    did; else the resolution-MISS ``raw_total``; else 0. Never raises.
    """
    if not isinstance(item, dict):
        return 0
    env = item.get("envelope_input")
    if not isinstance(env, dict):
        env = item
    data = env.get("data") if isinstance(env, dict) else None
    parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(parts, dict):
        return 0
    harm = parts.get("harmonized_query")
    if isinstance(harm, dict) and isinstance(harm.get("records"), list) and harm["records"]:
        return int(harm["total"]) if isinstance(harm.get("total"), int) else len(harm["records"])
    raw = parts.get("raw_query")
    if isinstance(raw, dict) and isinstance(raw.get("records"), list) and raw["records"]:
        return int(raw["total"]) if isinstance(raw.get("total"), int) else len(raw["records"])
    if isinstance(parts.get("raw_records"), list) and parts["raw_records"]:
        rt = parts.get("raw_total")
        return int(rt) if isinstance(rt, int) else len(parts["raw_records"])
    # No records on any leg — report the harmonized total if present (e.g. an all-empty index
    # legitimately has 0 available), else the raw total, else 0.
    for leg in (harm, raw):
        if isinstance(leg, dict) and isinstance(leg.get("total"), int):
            return leg["total"]
    rt = parts.get("raw_total")
    return rt if isinstance(rt, int) else 0


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
        per_index_available: dict[str, int] = {}
        for i, item in enumerate(items):
            recs = _records_from_item(item)
            label = index_names[i] if i < len(index_names) else f"index_{i}"
            per_index_counts[str(label)] = len(recs)
            per_index_available[str(label)] = _available_from_item(item)
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
            "per_index_available": per_index_available,
            # The full searched index set (all 9), so the report renders EVERY index — even one
            # that returned nothing — making "all indices searched (mandatory)" verifiable.
            "index_names": list(index_names),
            "total_records": len(globus_results),
            "map_errors": map_errors,
        }
        # Record the all-9-index search in the document's step progression (order -1 sorts it
        # ahead of the back-half stages: data_readiness=0, assemble=1, …). This is what puts
        # "searched all 9 indices" into the report's Analysis-steps trace.
        from apecx_integration.composition.steps._stage_report import append_stage_report

        searched = (
            ", ".join(
                f"{name} {per_index_counts.get(name, 0)}/{per_index_available.get(name, 0)}"
                for name in (index_names or per_index_counts)
            )
            if (index_names or per_index_counts)
            else "none"
        )
        append_stage_report(
            bundle,
            stage="harmonized_search",
            order=-1,
            markdown=f"Searched all {len(index_names or per_index_counts)} Globus indices "
            f"(used/available): {searched}.",
            data={
                "per_index_available": per_index_available,
                "per_index_kept": per_index_counts,
            },
        )

        # ``items`` (the per-index search envelopes) carried the full record sets the
        # loop just flattened into ``globus_results``. Nothing downstream of hmerge reads
        # ``items``, so drop it here — otherwise the entire corpus would ride a second,
        # dead copy through every remaining step (data_readiness → … → distill).
        bundle.pop("items", None)

        log.info(
            "HarmonizedBundleMergeStep %s: indices=%d total_records=%d taxon_id=%s map_errors=%s",
            self.name,
            len(items),
            len(globus_results),
            taxon_id,
            "set" if map_errors else "none",
        )
        return bundle
