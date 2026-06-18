"""TaxonCandidateReviewStep — LLM picks the right taxon, then a deterministic CDS-coverage gate.

The third (terminal) step of the OPTIONAL LLM-driven taxon-resolution fallback. Given the ranked
BV-BRC taxonomy candidates from ``BvbrcTaxonomySearchStep``, it asks the LLM which ONE candidate
is the SAME virus as the query, then VERIFIES the LLM's pick has real sequence coverage
(``genome_feature`` CDS count >= ``min_cds``) before promoting it. A pick that fails verification
— or no pick at all — is a NAMED miss, never a silently-wrong taxon.

On a win it FINALIZES the resolution onto the bundle exactly like the dict resolver would
(``canonical_iri`` / ``taxon_id`` / ``resolved_species_name`` / ``resolution_status`` /
``taxon_resolution``) so the downstream sequence leg + gate consume it uniformly. When the dict
resolver already won (``canonical_iri`` set), it only ensures ``taxon_id`` is the int form.

RELIABILITY: does NOT set ``LLM_ROLE`` (the OPTIONAL fallback must not force an LLM requirement).
FAIL-LOUD only on non-dict input; every data/network/LLM issue is a degrade-loud miss.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration._bounded_cache import BoundedDict
from apecx_integration.agents._llm_config import preflight_llm_model
from apecx_integration.agents._llm_factory import build_chat_llm
from apecx_integration.composition.steps._bvbrc_cds import cds_count
from apecx_integration.composition.steps.harmonized_search_execute_step import _iri_to_taxon_id
from apecx_integration.composition.steps.taxon_synonym_generation_step import _load_system_prompt

log = logging.getLogger(__name__)

_INPUT_KEY = "taxon_review_input"
_DEFAULT_PROMPT_FILENAME = "taxon_candidate_review_prompt.yml"

# Process-lifetime memo of the per-query verdict (taxon_id, or None for a miss). The fallback is
# expensive (LLM + several HTTP calls); an identical-query re-run reuses the verdict. Keyed by the
# normalized query string. ``_clear_cache`` lets unit tests reset it between cases. FIFO-bounded
# so a long-lived MCP server fielding many distinct queries does not grow without limit.
_REVIEW_CACHE: BoundedDict = BoundedDict(maxsize=512)


def _clear_cache() -> None:
    """Test seam: drop the process-lifetime per-query verdict cache."""
    _REVIEW_CACHE.clear()


def _already_resolved(bundle: dict[str, Any]) -> bool:
    """True when the deterministic dict resolver already won (the fallback is then skipped)."""
    iri = bundle.get("canonical_iri")
    return isinstance(iri, str) and "NCBITaxon" in iri


class TaxonCandidateReviewStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)
    min_cds: int = Field(
        default=2,
        ge=1,
        description="Minimum genome_feature CDS count a chosen taxon must have to be promoted "
        "(below this the pick is a NAMED miss — no sequence coverage to analyze).",
    )
    bvbrc_api_base: str = Field(default="https://www.bv-brc.org/api")
    request_timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class TaxonCandidateReviewStep(BaseStep):
    COMPONENT_TYPE: str = "taxon_candidate_review_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return TaxonCandidateReviewStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._min_cds: int = int(getattr(config, "min_cds", 2))
        self._api_base: str = getattr(config, "bvbrc_api_base", "https://www.bv-brc.org/api")
        self._timeout: float = float(getattr(config, "request_timeout_seconds", 60.0))
        self._system_prompt: str = _load_system_prompt(_DEFAULT_PROMPT_FILENAME)

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        bundle = dict(self._unwrap(input_data))

        # Dict resolver already won → just FINALIZE the int taxon_id from the IRI for the leg/gate.
        if _already_resolved(bundle):
            if not isinstance(bundle.get("taxon_id"), int):
                tid = _iri_to_taxon_id(bundle["canonical_iri"])
                if tid is not None:
                    bundle["taxon_id"] = tid
            return bundle

        query = bundle.get("query") or ""
        key = query.strip().lower()

        # Cached verdict for this exact query (None = a remembered miss).
        if key in _REVIEW_CACHE:
            cached = _REVIEW_CACHE[key]
            if cached is None:
                return self._set_miss(bundle, "no candidate matched the query (cached)")
            cand = self._find_candidate(bundle, cached)
            return self._finalize_winner(
                bundle,
                cached,
                cand.get("taxon_name", "") if cand else "",
                cand.get("genomes") if cand else None,
                cds=None,
            )

        candidates = bundle.get("taxon_candidates") or []
        if not candidates:
            return self._miss(bundle, key, "no BV-BRC taxonomy candidates to review")

        try:
            preflight_llm_model()
        except Exception as exc:  # noqa: BLE001 - optional LLM; degrade-loud, never raise
            log.warning("TaxonCandidateReviewStep %s: LLM preflight failed (%s)", self.name, exc)
            return self._miss(bundle, key, "LLM unavailable for candidate review")

        valid_ids = {c["taxon_id"] for c in candidates if isinstance(c.get("taxon_id"), int)}
        try:
            llm = build_chat_llm(temperature=0.0, max_tokens=64)
            listing = "\n".join(
                f"{c['taxon_id']} | {c.get('taxon_name', '')} "
                f"({c.get('genomes', 0)} genomes, {c.get('cds', 0)} CDS)"
                for c in candidates
                if isinstance(c.get("taxon_id"), int)
            )
            user = f"Query: {query}\n\nCandidates:\n{listing}"
            resp = await asyncio.to_thread(
                llm.invoke, [SystemMessage(content=self._system_prompt), HumanMessage(content=user)]
            )
            matched = self._parse_matches(getattr(resp, "content", "") or "", valid_ids)
        except Exception as exc:  # noqa: BLE001 - optional LLM; degrade-loud, never raise
            log.warning("TaxonCandidateReviewStep %s: candidate review failed (%s)", self.name, exc)
            return self._miss(bundle, key, f"candidate review failed ({type(exc).__name__})")

        if not matched:
            return self._miss(bundle, key, "no candidate matched the query")

        # COVERAGE-MAXIMIZING selection: among the candidates the LLM confirmed are the SAME virus,
        # pick the one with the MOST exact CDS (the fetchable level). A genus and its clades both
        # "match" the query, but only the covered clade yields sequences for the conservation leg —
        # the LLM judges identity, the step maximizes coverage deterministically.
        matched_cands = sorted(
            (c for c in candidates if c.get("taxon_id") in matched),
            key=lambda c: (-(c.get("cds") or 0), -(c.get("genomes") or 0), c["taxon_id"]),
        )
        winner = matched_cands[0]
        chosen = winner["taxon_id"]

        # Re-verify the winner's CDS coverage (freshness) before promoting.
        try:
            cds = await asyncio.to_thread(self._cds_count, chosen)
        except Exception as exc:  # noqa: BLE001 - degrade-loud; treat an unverifiable taxon as 0
            log.warning(
                "TaxonCandidateReviewStep %s: CDS verify for taxon %d failed (%s)",
                self.name,
                chosen,
                exc,
            )
            cds = winner.get("cds") or 0
        if cds < self._min_cds:
            return self._miss(
                bundle,
                key,
                f"best matching taxon {chosen} has {cds} CDS (< min_cds {self._min_cds})",
            )

        _REVIEW_CACHE[key] = chosen
        return self._finalize_winner(
            bundle, chosen, winner.get("taxon_name", ""), winner.get("genomes"), cds=cds
        )

    # ----- helpers -----
    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"TaxonCandidateReviewStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Single-key trigger-envelope unwrap (the framework delivers {taxon_review_input: payload}).
        if "query" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data

    @staticmethod
    def _find_candidate(bundle: dict[str, Any], taxon_id: int) -> dict[str, Any] | None:
        for c in bundle.get("taxon_candidates") or []:
            if isinstance(c, dict) and c.get("taxon_id") == taxon_id:
                return c
        return None

    @staticmethod
    def _parse_matches(text: str, valid_ids: set[int]) -> set[int]:
        """Return the SET of candidate taxon_ids the LLM listed as the same virus as the query.

        The genus AND its descendant clades/species are all "the virus", so the LLM may list
        several; the step then maximizes CDS coverage among them. The literal ``NONE`` and any
        unparseable / out-of-set output yield the empty set (a named miss).
        """
        return {int(tok) for tok in re.findall(r"\d+", text) if int(tok) in valid_ids}

    def _cds_count(self, taxon_id: int) -> int:
        """Exact ``genome_feature`` CDS count for a taxon (shared with the search step)."""
        return cds_count(self._api_base, taxon_id, self._timeout)

    def _finalize_winner(
        self,
        bundle: dict[str, Any],
        taxon_id: int,
        name: str,
        genomes: int | None,
        cds: int | None,
    ) -> dict[str, Any]:
        bundle["canonical_iri"] = f"http://purl.obolibrary.org/obo/NCBITaxon_{taxon_id}"
        bundle["taxon_id"] = taxon_id
        bundle["resolved_species_name"] = name
        bundle["resolution_status"] = "llm_fallback"
        bundle["taxon_resolution"] = {
            "source": "llm-fallback",
            "taxon_id": taxon_id,
            "scientific_name": name,
            "genomes": genomes,
            "cds": cds,
        }
        log.info(
            "TaxonCandidateReviewStep %s: resolved query -> taxon_id=%d (%r, genomes=%s, cds=%s)",
            self.name,
            taxon_id,
            name,
            genomes,
            cds,
        )
        return bundle

    @staticmethod
    def _set_miss(bundle: dict[str, Any], note: str) -> dict[str, Any]:
        bundle["taxon_resolution"] = {"source": "llm-fallback", "taxon_id": None, "note": note}
        return bundle

    def _miss(self, bundle: dict[str, Any], key: str, note: str) -> dict[str, Any]:
        _REVIEW_CACHE[key] = None
        log.warning("TaxonCandidateReviewStep %s: %s", self.name, note)
        return self._set_miss(bundle, note)


__all__ = ["TaxonCandidateReviewStep", "TaxonCandidateReviewStepConfig"]
