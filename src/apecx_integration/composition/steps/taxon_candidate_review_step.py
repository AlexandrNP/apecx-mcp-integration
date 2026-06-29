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

# An under-specified resolution: the fallback could only land on a non-specific UMBRELLA taxon
# (e.g. "Herpes simplex virus unknown type" for the ambiguous "herpes simplex virus", which spans
# HSV-1 vs HSV-2). Rather than silently analyze that poorly-defined taxon, the workflow returns a
# CLARIFICATION (needs_input) so the host LLM asks the user for a specific organism / taxon_id.
_UNDERSPECIFIED_TAXON_RE = re.compile(
    r"\b(unknown|unclassified|unidentified|unspecified|untyped)\b|\bsp\.", re.IGNORECASE
)


def _is_underspecified_taxon(name: str) -> bool:
    """True iff a resolved taxon name is a non-specific umbrella (so the analysis would run on a
    poorly-defined taxon) — the cue to ask the user to disambiguate instead of guessing."""
    return bool(name) and _UNDERSPECIFIED_TAXON_RE.search(name) is not None


def _most_specific(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop candidates that are an ANCESTOR of another candidate (their taxon_id appears in another's
    ``lineage_ids``), keeping the most-specific per lineage. So a genus + its OWN clade (Norovirus +
    Norovirus GII) collapses to the clade — NOT a false ambiguity — while true sibling species
    (HSV-1 + HSV-2, where neither is in the other's lineage) both remain."""
    out: list[dict[str, Any]] = []
    for c in cands:
        tid = c.get("taxon_id")
        if tid is not None and any(
            tid in (o.get("lineage_ids") or []) for o in cands if o is not c
        ):
            continue
        out.append(c)
    return out


def _distinct_species_reps(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One representative candidate per distinct SPECIES (input order preserved, after collapsing
    nested ancestors). len > 1 ⟺ the query spans multiple distinct viral species (ambiguous)."""
    reps: dict[int, dict[str, Any]] = {}
    for c in _most_specific(cands):
        sp = c.get("species_taxon_id") or c.get("taxon_id")
        if sp is not None and sp not in reps:
            reps[sp] = c
    return list(reps.values())


# A bare DISEASE/SYNDROME category that is NOT a single virus — multiple UNRELATED viral families cause
# it (hepatitis → HAV/HBV/HCV/HEV; encephalitis → JEV/TBEV/EEEV; viral hemorrhagic fever →
# Lassa/Ebola/CCHF/RVF). Stopword-anchored so a QUALIFIED name ("Japanese encephalitis virus",
# "Hepatitis B virus", "Crimean-Congo hemorrhagic fever virus") never matches — and those resolve via
# the dict and short-circuit before this step anyway. 2026-06-28 syndrome-ambiguity (the SAFE path,
# after the family-spread discriminator was found UNSAFE — specific HF viruses get "fever virus"
# synonyms). The family-spread no-go is recorded in docs/fresh_install_findings.md.
_SYNDROME_RE = re.compile(
    r"(?:^|\b(?:the|a|an|on|of|for|to|with|against|targeting)\s+)"
    r"(hepatitis|encephalitis|(?:viral\s+)?ha?emorrhagic\s+fever|respiratory|gastroenteritis)"
    r"\s+vir(?:us|al)\b",
    re.IGNORECASE,
)


def _syndrome_category(query: str) -> str | None:
    """The bare disease-category term in a query ("...the hepatitis virus...") that is NOT a specific
    virus, or None. Used to ask the user to disambiguate instead of silently analyzing one member."""
    if not isinstance(query, str):
        return None
    m = _SYNDROME_RE.search(query)
    return m.group(1).lower() if m else None


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
    # OPTIONAL LLM fallback (degrade-loud); does NOT REQUIRE a server LLM. Declared 'none'
    # explicitly so the workflow_requires_llm heuristic doesn't mis-flag it as in-DAG and force a
    # server-LLM requirement in desktop locus. See the module docstring + test_llm_policy.
    LLM_ROLE: str = "none"

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

        # Bare SYNDROME term (a disease category, not a virus) → CLARIFY instead of the LLM fallback
        # silently picking one arbitrary member (the synonym step collapses "hepatitis virus" → HBV).
        # Only reached on a dict MISS (specific viruses dict-resolve + returned above).
        syndrome = _syndrome_category(query)
        if syndrome:
            return self._needs_clarification_syndrome(bundle, syndrome)

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

        # Broadened ambiguity (2026-06-27): if the LLM-confirmed candidates span MULTIPLE DISTINCT
        # viral SPECIES (true siblings, not a genus + its own clade), the query is ambiguous — e.g.
        # "hepatitis virus" → Hep A/B/C, "herpes simplex virus" → HSV-1/HSV-2. Ask the user to choose
        # (listing the species) instead of silently picking the highest-coverage one.
        species_reps = _distinct_species_reps(matched_cands)
        if len(species_reps) > 1:
            return self._needs_clarification_multi(bundle, species_reps)

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
        # Under-specified umbrella taxon (e.g. "...unknown type") → do NOT analyze a poorly-defined
        # taxon; return a CLARIFICATION (needs_input) so the host LLM asks the user to disambiguate.
        if _is_underspecified_taxon(name):
            return self._needs_clarification(bundle, name, taxon_id)
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

    def _needs_clarification(
        self, bundle: dict[str, Any], name: str, taxon_id: int
    ) -> dict[str, Any]:
        """Emit a needs_input CLARIFICATION: the request resolved only to an under-specified umbrella
        taxon. Sets a ``control_transfer`` (ambiguous_entity) for the terminal EnvelopeStep to surface
        as ``status=needs_input``, and marks the resolution a miss so the analysis legs fast-degrade
        rather than run on a poorly-defined taxon."""
        from apecx_integration.composition.schemas.control_transfer import ambiguous_entity_transfer

        query = (bundle.get("query") or "").strip()
        msg = (
            f"The request resolved only to an UNDER-SPECIFIED taxon — {name!r} (taxon {taxon_id}). "
            f"This name is ambiguous (it spans multiple types/strains — e.g. HSV-1 vs HSV-2 for "
            f"'herpes simplex virus'), so a meaningful epitope analysis cannot be run on it. Re-call "
            f"viral_epitope_analysis with a SPECIFIC organism name (a type or strain) or an explicit "
            f"taxon_id."
        )
        bundle["control_transfer"] = ambiguous_entity_transfer([], message=msg).model_dump(
            mode="json"
        )
        log.warning(
            "TaxonCandidateReviewStep %s: under-specified taxon %r (%d) for query %r — "
            "requesting clarification (needs_input)",
            self.name,
            name,
            taxon_id,
            query,
        )
        return self._set_miss(
            bundle, f"under-specified taxon {name!r} ({taxon_id}) — clarification requested"
        )

    def _needs_clarification_multi(
        self, bundle: dict[str, Any], reps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Clarification for a query matching MULTIPLE distinct viral species — list them as candidate
        IRIs/labels so the host LLM asks the user which one, and mark a miss so the legs fast-degrade
        (vs silently analyzing the highest-coverage species)."""
        from apecx_integration.composition.schemas.control_transfer import ambiguous_entity_transfer

        candidates = [
            {
                "canonical_iri": (
                    "http://purl.obolibrary.org/obo/NCBITaxon_"
                    f"{r.get('species_taxon_id') or r.get('taxon_id')}"
                ),
                "label": r.get("taxon_name", ""),
                "genomes": r.get("genomes"),
                "cds": r.get("cds"),
            }
            for r in reps[:8]
        ]
        labels = ", ".join(c["label"] for c in candidates if c["label"])
        msg = (
            f"The request is AMBIGUOUS — it matches {len(candidates)} distinct viral species: "
            f"{labels}. Re-call viral_epitope_analysis with a SPECIFIC organism (one of these) or an "
            f"explicit taxon_id so a meaningful epitope analysis can run."
        )
        bundle["control_transfer"] = ambiguous_entity_transfer(candidates, message=msg).model_dump(
            mode="json"
        )
        log.warning(
            "TaxonCandidateReviewStep %s: ambiguous — %d distinct viral species (%s) — "
            "requesting clarification",
            self.name,
            len(candidates),
            labels[:80],
        )
        return self._set_miss(
            bundle,
            f"ambiguous — {len(candidates)} distinct viral species — clarification requested",
        )

    def _needs_clarification_syndrome(
        self, bundle: dict[str, Any], category: str
    ) -> dict[str, Any]:
        """Clarification for a bare DISEASE/SYNDROME query — name the category, give per-syndrome
        examples so the host LLM can ask the user for a specific virus, and mark a miss."""
        from apecx_integration.composition.schemas.control_transfer import ambiguous_entity_transfer

        examples = {
            "hepatitis": "Hepatitis A / B / C / E virus (HAV/HBV/HCV/HEV)",
            "encephalitis": "Japanese / tick-borne / Eastern equine encephalitis virus",
            "respiratory": "RSV, influenza A, SARS-CoV-2, a specific coronavirus",
            "gastroenteritis": "norovirus, rotavirus, a specific astrovirus",
        }.get(category, "Lassa, Ebola, Crimean-Congo, or Rift Valley fever virus")
        msg = (
            f"{category.title()!r} names a DISEASE/SYNDROME, not a single virus — multiple UNRELATED "
            f"viruses cause it (e.g. {examples}). Re-call viral_epitope_analysis with a SPECIFIC virus "
            f"(scientific name, acronym, or NCBI taxon_id)."
        )
        bundle["control_transfer"] = ambiguous_entity_transfer([], message=msg).model_dump(
            mode="json"
        )
        log.warning(
            "TaxonCandidateReviewStep %s: bare syndrome term %r — requesting clarification",
            self.name,
            category,
        )
        return self._set_miss(bundle, f"syndrome category {category!r} — clarification requested")

    @staticmethod
    def _set_miss(bundle: dict[str, Any], note: str) -> dict[str, Any]:
        bundle["taxon_resolution"] = {"source": "llm-fallback", "taxon_id": None, "note": note}
        return bundle

    def _miss(self, bundle: dict[str, Any], key: str, note: str) -> dict[str, Any]:
        _REVIEW_CACHE[key] = None
        log.warning("TaxonCandidateReviewStep %s: %s", self.name, note)
        return self._set_miss(bundle, note)


__all__ = ["TaxonCandidateReviewStep", "TaxonCandidateReviewStepConfig"]
