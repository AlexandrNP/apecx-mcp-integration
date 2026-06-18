"""BvbrcTaxonomySearchStep — DETERMINISTIC BV-BRC taxonomy lookup over candidate names.

The second step of the OPTIONAL LLM-driven taxon-resolution fallback. Given the candidate virus
names produced by ``TaxonSynonymGenerationStep`` (``bundle['taxon_synonyms']``), it queries the
live BV-BRC ``taxonomy`` index for each name and aggregates the matching taxa into a ranked
candidate list for the LLM reviewer (step 3) to pick from. NO LLM here — purely the name → taxon
HTTP lookups.

RELIABILITY: FAIL-LOUD only on non-dict input. A per-synonym network/parse error is logged and
SKIPPED (never raises) — a fallback step must never break the run on a data/network issue.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import requests
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

_INPUT_KEY = "bvbrc_search_input"
# Rows pulled per synonym from BV-BRC taxonomy (best-genome-coverage taxa first).
_PER_SYNONYM_LIMIT = 5


def _already_resolved(bundle: dict[str, Any]) -> bool:
    """True when the deterministic dict resolver already won (the fallback is then skipped)."""
    iri = bundle.get("canonical_iri")
    return isinstance(iri, str) and "NCBITaxon" in iri


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class BvbrcTaxonomySearchStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    max_candidates: int = Field(default=5, ge=1)
    bvbrc_api_base: str = Field(default="https://www.bv-brc.org/api")
    request_timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class BvbrcTaxonomySearchStep(BaseStep):
    COMPONENT_TYPE: str = "bvbrc_taxonomy_search_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return BvbrcTaxonomySearchStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_candidates: int = int(getattr(config, "max_candidates", 5))
        self._api_base: str = getattr(config, "bvbrc_api_base", "https://www.bv-brc.org/api")
        self._timeout: float = float(getattr(config, "request_timeout_seconds", 60.0))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        bundle = dict(self._unwrap(input_data))
        if _already_resolved(bundle):
            return bundle

        synonyms = bundle.get("taxon_synonyms") or []
        # taxon_id -> {"taxon_id", "taxon_name", "hits"}; keep the name from the max-genomes row.
        agg: dict[int, dict[str, Any]] = {}
        for syn in synonyms:
            if not isinstance(syn, str) or not syn.strip():
                continue
            query = (
                f"eq(taxon_name,{quote(syn.strip())})"
                f"&select(taxon_id,taxon_name,genomes)"
                f"&sort(-genomes)"
                f"&limit({_PER_SYNONYM_LIMIT})"
            )
            try:
                rows = await asyncio.to_thread(self._get_json, "taxonomy", query)
            except Exception as exc:  # noqa: BLE001 - per-synonym best effort; skip, never raise
                log.warning(
                    "BvbrcTaxonomySearchStep %s: taxonomy lookup for %r failed (%s); skipping.",
                    self.name,
                    syn,
                    exc,
                )
                continue
            for r in rows:
                if not isinstance(r, dict):
                    continue
                tid = _as_int(r.get("taxon_id"))
                if tid is None:
                    continue
                genomes = _as_int(r.get("genomes")) or 0
                name = r.get("taxon_name") or ""
                cur = agg.get(tid)
                if cur is None or genomes > cur["hits"]:
                    agg[tid] = {"taxon_id": tid, "taxon_name": name, "hits": genomes}

        # Rank by genome coverage desc, tie-break taxon_id asc; keep top max_candidates.
        ranked = sorted(agg.values(), key=lambda c: (-c["hits"], c["taxon_id"]))
        bundle["taxon_candidates"] = ranked[: self._max_candidates]
        log.info(
            "BvbrcTaxonomySearchStep %s: %d synonym(s) -> %d distinct taxa -> %d candidate(s)",
            self.name,
            len(synonyms),
            len(agg),
            len(bundle["taxon_candidates"]),
        )
        return bundle

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"BvbrcTaxonomySearchStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Single-key trigger-envelope unwrap (the framework delivers {bvbrc_search_input: payload}).
        if "query" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data

    def _get_json(self, path: str, query: str) -> list[dict[str, Any]]:
        url = f"{self._api_base}/{path}/?{query}&http_accept=application/json"
        resp = requests.get(url, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(
                f"BvbrcTaxonomySearchStep '{self.name}': unexpected BV-BRC response shape from "
                f"{path}: {type(data).__name__}"
            )
        return data


__all__ = ["BvbrcTaxonomySearchStep", "BvbrcTaxonomySearchStepConfig"]
