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

from apecx_integration.composition.steps._bvbrc_cds import cds_count

log = logging.getLogger(__name__)

_INPUT_KEY = "bvbrc_search_input"
# Rows pulled per synonym from BV-BRC taxonomy (best-genome-coverage taxa first).
_PER_SYNONYM_LIMIT = 5
# Cap on how many distinct taxa get an exact-CDS probe (one HTTP call each), to bound cost on a
# many-synonym run. The cap is applied to the genome-coverage-ranked taxa, so the richest are probed.
_CDS_PROBE_CAP = 12


def _already_resolved(bundle: dict[str, Any]) -> bool:
    """True when the deterministic dict resolver already won (the fallback is then skipped)."""
    iri = bundle.get("canonical_iri")
    return isinstance(iri, str) and "NCBITaxon" in iri


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _species_taxon_id(row: dict[str, Any]) -> int | None:
    """The SPECIES-rank ancestor taxon_id from a BV-BRC taxonomy row's lineage (``lineage_ids`` zipped
    with ``lineage_ranks``). None when the lineage lacks a species rank — the caller falls back to the
    taxon_id itself. Robust species identity for ambiguity detection (no name-stripping)."""
    ids = row.get("lineage_ids") or []
    ranks = row.get("lineage_ranks") or []
    for tid, rank in zip(ids, ranks, strict=False):
        if rank == "species":
            return _as_int(tid)
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
        # taxon_id -> {"taxon_id", "taxon_name", "genomes"}; keep the name from the max-genomes row.
        agg: dict[int, dict[str, Any]] = {}
        for syn in synonyms:
            if not isinstance(syn, str) or not syn.strip():
                continue
            # eq(taxon_name,...) is Solr keyword-matched, so a short synonym ("HSV", "HHV") matches
            # NON-VIRAL taxa whose names merely contain the token — plants (Radula sp. HSV…),
            # synthetic constructs (Expression vector …/HSV1 tk), environmental bacteria. Constrain
            # server-side to the Viruses division so only real viruses enter the candidate list
            # (the downstream LLM then picks the right virus among them). 2026-06-27 pollution fix.
            query = (
                f"eq(taxon_name,{quote(syn.strip())})"
                f"&eq(division,Viruses)"
                f"&select(taxon_id,taxon_name,genomes,lineage_ids,lineage_ranks)"
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
                # The SPECIES-rank ancestor (from the taxonomy lineage) — the level at which two
                # candidates are "different viruses" vs strains of one. Lets the review step detect a
                # genuinely AMBIGUOUS query (candidates spanning >1 species → ask the user) robustly,
                # without fragile name-stripping (HSV "type 1/2" are distinct species; influenza
                # "strain X/Y" are not).
                species = _species_taxon_id(r) or tid
                lineage_ids = [i for i in (_as_int(x) for x in (r.get("lineage_ids") or [])) if i]
                cur = agg.get(tid)
                if cur is None or genomes > cur["genomes"]:
                    agg[tid] = {
                        "taxon_id": tid,
                        "taxon_name": name,
                        "genomes": genomes,
                        "species_taxon_id": species,
                        # full ancestor chain — lets the review step drop a genus when its own clade is
                        # also a candidate (nested ≠ ambiguous), so only true SIBLING species clarify.
                        "lineage_ids": lineage_ids,
                    }

        # COVERAGE-MAXIMIZING rank: a genus can have the most GENOMES yet ~0 fetchable CDS at the
        # exact taxon (the conservation leg fetches eq(taxon_id,X)&CDS), while a descendant
        # clade/species holds the actual CDS (e.g. genus Norovirus 0 CDS vs clade GII 112k). So
        # probe exact CDS for the top genome-coverage taxa and rank by CDS — surfacing the covered
        # clade over the thin genus. Probes are bounded to _CDS_PROBE_CAP; a per-taxon error → cds=0.
        by_genomes = sorted(agg.values(), key=lambda c: (-c["genomes"], c["taxon_id"]))
        for i, cand in enumerate(by_genomes):
            if i >= _CDS_PROBE_CAP:
                cand["cds"] = 0  # beyond the probe cap (lowest genome coverage) — not CDS-ranked
                continue
            try:
                cand["cds"] = await asyncio.to_thread(self._cds_count, cand["taxon_id"])
            except Exception as exc:  # noqa: BLE001 - degrade-loud; unverifiable taxon ranks last
                log.warning(
                    "BvbrcTaxonomySearchStep %s: CDS probe for taxon %d failed (%s); cds=0.",
                    self.name,
                    cand["taxon_id"],
                    exc,
                )
                cand["cds"] = 0
        ranked = sorted(by_genomes, key=lambda c: (-c["cds"], -c["genomes"], c["taxon_id"]))
        bundle["taxon_candidates"] = ranked[: self._max_candidates]
        log.info(
            "BvbrcTaxonomySearchStep %s: %d synonym(s) -> %d distinct taxa -> %d candidate(s) "
            "(top: %s)",
            self.name,
            len(synonyms),
            len(agg),
            len(bundle["taxon_candidates"]),
            bundle["taxon_candidates"][0] if bundle["taxon_candidates"] else None,
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

    def _cds_count(self, taxon_id: int) -> int:
        """Exact ``genome_feature`` CDS count for a taxon (shared with the review step)."""
        return cds_count(self._api_base, taxon_id, self._timeout)


__all__ = ["BvbrcTaxonomySearchStep", "BvbrcTaxonomySearchStepConfig"]
