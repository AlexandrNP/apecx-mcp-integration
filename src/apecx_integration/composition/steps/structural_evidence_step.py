"""StructuralEvidenceStep — pull PDB + EMDB structural records for the query.

The structural-evidence leg of ``viral_epitope_evidence_review``. It sits between
the synthesis-context assembly and the evidence-review synthesis: it queries the
aggregate APECx Globus Search index (``e74bf12a``) for structural records, using
the verified ``publisher.name`` discriminator to separate the two logical
sources — ``"RCSB PDB"`` and ``"Electron Microscopy Data Bank"`` — and merges the
hits into the bundle's ``globus_results`` so the downstream synthesizer cites
them natively (``[Globus pdb:1I9G]`` / ``[Globus emdb:EMD-34119]``).

No-silent-failure contract (the whole reason this is a separate step):

- A query that finds NO structural records sets a non-None ``structural_note``
  naming the entity and the empty result — the downstream step renders it as an
  explicit "no structural records found" limitation. An empty list is NEVER
  mistaken for "no structures exist": the absence is *named*.
- A Globus outage (``GlobusSearchUnavailableError``) ALSO sets a loud
  ``structural_note`` ("structural lookup unavailable: …") — distinct from a
  legitimate no-hit. The step degrades, it does not crash the workflow, and it
  does not pretend the lookup succeeded with zero hits.

Input contract (the bundle emitted by ``SynthesisContextAssemblyStep``)::

    {"query": str, "globus_results": list[dict], ...other source keys...}

Output: the same bundle, with ``globus_results`` extended (deduped by
``subject``) by the structural hits, PLUS two new keys consumed by
``EvidenceReviewSynthesisStep``::

    {..bundle.., "structural_records": list[dict], "structural_note": str | None}

Authoring rule alignment (nanobrain-step-authoring skill): ``process()`` only,
``from_config`` only, ``COMPONENT_TYPE`` + ``REQUIRED_CONFIG_FIELDS`` declared,
fail-fast on bad input shape.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

# Verified 2026-06-12 (Phase 0 probe of e74bf12a): publisher.name is a clean,
# server-side-filterable discriminator. PDB → 27,407 records, EMDB → 8,360.
_DEFAULT_PUBLISHERS: dict[str, str] = {
    "pdb": "RCSB PDB",
    "emdb": "Electron Microscopy Data Bank",
}

_INPUT_KEY = "structural_input"


class StructuralEvidenceStepConfig(StepConfig):
    """Config for StructuralEvidenceStep.

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

    max_per_source: int = Field(
        default=8,
        description="Hard cap on structural hits fetched per source (PDB, EMDB).",
    )


class StructuralEvidenceStep(BaseStep):
    COMPONENT_TYPE: str = "structural_evidence_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return StructuralEvidenceStepConfig

    @classmethod
    def extract_component_config(cls, config: StructuralEvidenceStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {**base, "max_per_source": getattr(config, "max_per_source", 8)}

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._max_per_source: int = int(component_config.get("max_per_source", 8))
        self._publishers: dict[str, str] = dict(_DEFAULT_PUBLISHERS)

    def _search_source(
        self,
        query: str,
        source: str,
        publisher: str,
        taxon_id: int | str | None,
        species_name: str | None,
    ):
        """Taxon-precise structural query for one source (E3-2). Delegates to the
        shared ``structural_query.search_one_source`` so the workflow leg and the
        MCP tool stay in lockstep. Lazy import keeps the globus_sdk dependency off
        the import path for offline/test environments.

        ``species_name`` is the canonical species spelling resolved upstream by the
        BV-BRC taxonomy resolver (arbitrary viruses whose taxon is not in the curated
        map); it lets the facet pre-pass taxon-lock SARS-CoV-2 / influenza / HIV etc.
        even though their taxon_id is not in ``_TAXON_SPECIES``."""
        from apecx_integration.agents.globus_search import structural_query

        return structural_query.search_one_source(
            query,
            source,
            publisher,
            taxon_id=taxon_id,
            species_name=species_name,
            max_results=self._max_per_source,
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"StructuralEvidenceStep '{self.name}': input_data must be a dict, got "
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

        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"StructuralEvidenceStep '{self.name}': bundle must carry a non-empty "
                f"'query' string; got {type(query).__name__}={query!r}"
            )
        query = query.strip()

        # taxon_id rides along on the bundle from the normalize step; it taxon-locks
        # the structural query (E3-2). Absent/non-taxon queries fall to query-text
        # parsing inside the shared core, then to a named degrade. resolved_species_name
        # is the canonical spelling the BV-BRC taxonomy resolver produced for an arbitrary
        # virus (taxon not in the curated map) — it taxon-locks the facet pre-pass for
        # SARS-CoV-2 / influenza / HIV whose taxon_id _TAXON_SPECIES does not carry.
        taxon_id = input_data.get("taxon_id")
        species_name = input_data.get("resolved_species_name")

        bundle = dict(input_data)  # shallow copy; we extend globus_results + add keys

        structural_records: list[dict[str, Any]] = []
        structural_note: str | None = None
        degrade_notes: list[str] = []
        # E3-8: capture the structural query ACTUALLY ISSUED per source — the resolved
        # organism spellings, the keyword query string, and the hit count — so a run's
        # provenance can record exactly what structural retrieval ran (these are computed
        # by structural_query.search_one_source and would otherwise be dropped here).
        query_per_source: dict[str, dict[str, Any]] = {}
        try:
            # Two independent source queries, concurrently (sync client offloaded).
            sources = list(self._publishers.items())
            results = await asyncio.gather(
                *(
                    asyncio.to_thread(self._search_source, query, src, pub, taxon_id, species_name)
                    for src, pub in sources
                )
            )
            for (src, _pub), result in zip(sources, results, strict=True):
                structural_records.extend(result.hits)
                if result.note:
                    degrade_notes.append(result.note)
                query_per_source[src] = {
                    "n_hits": len(result.hits),
                    "organisms": list(getattr(result, "organisms", []) or []),
                    "query_used": getattr(result, "query_used", "") or "",
                    "note": result.note,
                }
        except Exception as exc:  # GlobusSearchUnavailableError + any SDK/network error
            # LOUD degrade — distinct from a legitimate no-hit. The lookup did not
            # succeed-with-zero; it failed, and the output says so.
            structural_note = (
                f"Structural lookup unavailable ({type(exc).__name__}): {exc}. "
                f"PDB/EMDB evidence could not be retrieved for {query!r}."
            )
            log.warning("StructuralEvidenceStep %s: %s", self.name, structural_note)

        if structural_note is None and not structural_records:
            # LOUD no-hit — the absence is named, never a silent empty list.
            structural_note = (
                f"No PDB or EMDB structural records were found for {query!r} in the "
                f"APECx structural corpus."
            )
            log.info("StructuralEvidenceStep %s: %s", self.name, structural_note)
        elif structural_note is None and degrade_notes:
            # Records present, but the query could NOT be taxon-locked — name it,
            # never a silent unfiltered dump (E3-2.5).
            structural_note = " ".join(dict.fromkeys(degrade_notes))
            log.warning("StructuralEvidenceStep %s: %s", self.name, structural_note)

        # Merge structural hits into globus_results for native citation, deduped
        # by subject (the assembly step's unfiltered Globus branch may already
        # carry some of the same structural records).
        existing = bundle.get("globus_results") or []
        if not isinstance(existing, list):
            existing = []
        seen = {h.get("subject") for h in existing if isinstance(h, dict)}
        merged = list(existing)
        for h in structural_records:
            subj = h.get("subject") if isinstance(h, dict) else None
            if subj and subj not in seen:
                seen.add(subj)
                merged.append(h)
        bundle["globus_results"] = merged

        bundle["structural_records"] = structural_records
        bundle["structural_note"] = structural_note
        # E3-8 provenance: the issued structural query (resolved organisms + keyword query
        # + per-source hit counts). Empty per_source on an outage — the note names why.
        bundle["structural_query"] = {
            "taxon_id": taxon_id,
            "per_source": query_per_source,
            "note": structural_note,
        }

        # Stage-report scaffolding (E2-C): document the structural-evidence stage's
        # contribution (records found, or the loud no-hit/outage note).
        from apecx_integration.composition.steps._stage_report import append_stage_report

        append_stage_report(
            bundle,
            stage="structural_evidence",
            order=2,
            markdown=(
                structural_note
                if structural_note
                else f"Retrieved {len(structural_records)} PDB/EMDB structural record(s)."
            ),
            data={
                "structural_records": len(structural_records),
                "note": structural_note,
            },
        )
        log.info(
            "StructuralEvidenceStep %s: query=%.80r structural_hits=%d note=%s",
            self.name,
            query,
            len(structural_records),
            "set" if structural_note else "none",
        )
        return bundle
