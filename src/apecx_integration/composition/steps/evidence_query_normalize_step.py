"""EvidenceQueryNormalizeStep — the first step of viral_epitope_analysis.

Its sole job is to be the deposit point that captures the workflow's input params
and fans them out to BOTH the synthesis pipeline (which needs ``query``) AND the
terminal design gate (which needs ``requested_outputs`` / ``design_approval_id``).

WHY THIS STEP EXISTS (load-bearing — a subtle silent-failure fix):
``run_workflow`` deposits the input under the catalog's ``input_envelope_key``,
which is THIS step's input DU (``normalize_input``) — NOT the workflow-level
``workflow_input`` DU. So control state cannot be fanned out from ``workflow_input``
(it never gets set on the run_workflow path). Instead it must be captured here, at
the deposit point, and fanned out from THIS step's real OUTPUT DU. The reused
``SynthesisContextAssemblyStep`` (and the synthesis steps) then drop the control
fields from their outputs — which is fine, because the gate gets them via the
separate fan-out edge ``normalize → gate.control_in``.

Input contract (after the framework ``{normalize_input: payload}`` unwrap):
``{query, taxon_id?, protein?, requested_outputs?, design_approval_id?}``.
Output: the SAME dict, unchanged — a deliberate passthrough so one output DU feeds
both ``assemble`` (reads ``query``) and ``gate`` (reads the control fields).
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

_INPUT_KEY = "normalize_input"


class EvidenceQueryNormalizeStepConfig(StepConfig):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class EvidenceQueryNormalizeStep(BaseStep):
    COMPONENT_TYPE: str = "evidence_query_normalize_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return EvidenceQueryNormalizeStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"EvidenceQueryNormalizeStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Unwrap the framework trigger envelope ({normalize_input: payload}); direct
        # callers (tests) pass the payload raw.
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"EvidenceQueryNormalizeStep '{self.name}': input must carry a non-empty "
                f"'query' string; got {type(query).__name__}={query!r}"
            )
        # Passthrough: one output DU feeds both assemble (query) and gate (control fields).
        out = dict(input_data)
        # Resolve an ARBITRARY virus name from the query text to a real BV-BRC taxon_id +
        # canonical species name when the caller did not hand one in. This is what unlocks
        # FULL science (sequence conservation + structural reasoning + functional validation)
        # for viruses outside the curated 4 — e.g. SARS-CoV-2 / influenza / HIV. The resolved
        # taxon_id flows to the BV-BRC sequence fetch (sequence.sequence_params) and the
        # canonical name flows to the structural facet (via assemble -> structural). DEGRADE-
        # LOUD (G127): the resolver never raises; an unresolvable name leaves taxon_id absent
        # and records a NAMED note (the existing degrade-loud legs then state the absence).
        await self._maybe_resolve_taxon(query, out)
        log.info(
            "EvidenceQueryNormalizeStep %s: query=%.60r taxon_id=%r requested_outputs=%r",
            self.name,
            query,
            out.get("taxon_id"),
            out.get("requested_outputs"),
        )
        return out

    async def _maybe_resolve_taxon(self, query: str, out: dict[str, Any]) -> None:
        """Resolve the query's virus name to a BV-BRC taxon when no usable taxon_id was given.

        Mutates ``out`` in place: on success sets ``taxon_id`` (NCBI id with BV-BRC sequence
        coverage), ``resolved_species_name`` (canonical spelling for the structural facet), and
        a ``taxon_resolution`` provenance block. On failure sets only the provenance block with
        a NAMED note. A caller-supplied usable ``taxon_id`` is left untouched (never overridden).
        """
        import asyncio

        existing = out.get("taxon_id")
        if isinstance(existing, int) or (isinstance(existing, str) and existing.strip().isdigit()):
            # Caller hand-supplied a taxon_id — honour it untouched (clean passthrough).
            return

        from apecx_integration.agents.globus_search import taxonomy_resolver
        from apecx_integration.composition.steps.harmonized_resolve_step import (
            build_resolution_plan,
        )
        from apecx_integration.composition.steps.harmonized_search_execute_step import (
            _iri_to_taxon_id,
        )

        candidates = taxonomy_resolver.extract_virus_names(query)
        term = candidates[0] if candidates else query
        # Resolve via the SAME dict resolver and SAME term-selection as EpitopeResolveStep, so the
        # sequence leg's taxon AGREES with the recorded bundle["taxon_id"] (harmonized_bundle_merge
        # also derives from this plan's canonical_iri). The dict applies merged_taxons redirects
        # (e.g. Lassa 11620→3052310) — the live-BV-BRC name-matcher used before did not, and it
        # fuzzy-matched wrong organisms (Junin→an influenza strain). Coverage is NOT pre-checked
        # here; a resolved-but-empty taxon (e.g. norovirus) is surfaced loudly downstream by the
        # BvbrcProteinFastaStep too-few-sequences fallback.
        plan = await asyncio.to_thread(
            build_resolution_plan, term, index="bvbrc_genome", entity_type_str=""
        )
        iri = plan.get("canonical_iri")
        taxon_id = _iri_to_taxon_id(iri) if isinstance(iri, str) and iri else None
        if taxon_id is None:
            out["taxon_resolution"] = {
                "source": "synonym-dictionary",
                "taxon_id": None,
                "candidates": candidates,
                "resolution_status": plan.get("resolution_status"),
                "note": (
                    f"no taxon resolved: the synonym dictionary could not map a virus name from the "
                    f"query {query!r} to an NCBI taxon (candidates tried: "
                    f"{candidates or 'none extracted'}). Sequence conservation, structural "
                    "reasoning, and functional validation are unavailable for this run; literature "
                    "evidence still proceeds."
                ),
            }
            log.warning(
                "EvidenceQueryNormalizeStep %s: %s",
                self.name,
                out["taxon_resolution"]["note"],
            )
            return

        out["taxon_id"] = taxon_id
        out["resolved_species_name"] = plan.get("canonical_label")
        out["taxon_resolution"] = {
            "source": "synonym-dictionary",
            "taxon_id": taxon_id,
            "scientific_name": plan.get("canonical_label"),
            "canonical_iri": iri,
            "resolution_status": plan.get("resolution_status"),
            "candidates": candidates,
        }
        log.info(
            "EvidenceQueryNormalizeStep %s: resolved %r -> taxon_id=%d (%r, status=%s)",
            self.name,
            term,
            taxon_id,
            plan.get("canonical_label"),
            plan.get("resolution_status"),
        )
