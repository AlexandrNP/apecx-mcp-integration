"""EpitopeResolveStep — resolve a BARE virus name from the query into a canonical
plan + the full destination-index fan-out list, for the harmonized
``viral_epitope_analysis`` path.

First step of the harmonized epitope path. It takes the scientist bundle
(``{"query": ..., optional "protein"/"taxon_id"}``), extracts a virus name
from ``query``, resolves it once to a canonical entity (via the shared
``build_resolution_plan`` helper), and emits the bundle augmented with:

- the FLATTENED plan fields at top level (EXCEPT ``index`` — a downstream
  map spreads these as statics and sets a per-index ``index`` itself):
  ``term``, ``resolution_path``, ``canonical_iri``, ``canonical_label``,
  ``canonical_ontology``, ``confidence``, ``resolution_status``,
  ``synonyms``, ``candidates``, ``needs_disambiguation``, ``evidence``;
- ``resolution_plan`` — the full plan dict (for the merge step's taxon
  derivation);
- ``index_names`` — the 9 Globus DESTINATION index short names
  (``sorted(_INDEX_UUIDS)``) the downstream map fans out across.

Degrade-loud contract:

- An AMBIGUOUS resolution sets ``index_names = []`` (the map no-ops — running
  the harmonized search across heterogeneous candidate taxa would lump
  unrelated biology together), records ``_ambiguous_candidates`` and a loud
  ``resolution_note``.
- A resolution that FAILS to run (dict/network error) does NOT raise — it
  records a loud ``resolution_note`` and a miss-shaped plan, and KEEPS the
  full ``index_names`` fan-out so the per-index RAW fallback still pulls any
  records present in each index.
- Only a non-dict input or a missing/empty ``query`` is fatal (``ValueError``,
  fail-fast on contract — mirrors ``StructuralEvidenceStep``).

Authoring rule alignment (nanobrain-step-authoring skill): ``process()`` only,
``from_config`` only, ``COMPONENT_TYPE`` + ``REQUIRED_CONFIG_FIELDS`` declared.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

_INPUT_KEY = "resolve_input"

# The plan fields spread onto the bundle's top level, EXCLUDING ``index``
# (the downstream map sets a per-index ``index`` itself).
_FLATTENED_PLAN_KEYS = (
    "term",
    "resolution_path",
    "canonical_iri",
    "canonical_label",
    "canonical_ontology",
    "confidence",
    "resolution_status",
    "synonyms",
    "candidates",
    "needs_disambiguation",
    "evidence",
)


class EpitopeResolveStepConfig(StepConfig):
    """Config for EpitopeResolveStep.

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


class EpitopeResolveStep(BaseStep):
    COMPONENT_TYPE: str = "epitope_resolve_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return EpitopeResolveStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"EpitopeResolveStep '{self.name}': input_data must be a dict, got "
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
                f"EpitopeResolveStep '{self.name}': bundle must carry a non-empty "
                f"'query' string; got {type(query).__name__}={query!r}"
            )
        query = query.strip()

        bundle = dict(input_data)  # shallow copy; we add the plan fields

        # Lazy imports keep heavy modules off the import path for offline tests.
        from apecx_integration.agents.globus_search import taxonomy_resolver
        from apecx_integration.composition.steps.harmonized_resolve_step import (
            build_resolution_plan,
        )
        from apecx_integration.composition.steps.harmonized_search_execute_step import (
            _INDEX_UUIDS,
        )

        names = taxonomy_resolver.extract_virus_names(query)
        term = names[0] if names else query

        index_names = sorted(_INDEX_UUIDS)
        resolution_note: str | None = None

        # Honor a pre-resolved taxon (caller-supplied taxon_id seeded by normalize as canonical_iri):
        # SKIP the dict name-resolution but fall through to the SHARED flatten + index_names + stage
        # setup below (the harmonized map needs index_names + resolution_plan — an early return here
        # silently emptied the 9-index search). The LLM fallback chain also short-circuits on this.
        _pre_iri = bundle.get("canonical_iri")
        if isinstance(_pre_iri, str) and "NCBITaxon" in _pre_iri:
            plan = {
                "term": term,
                "index": "bvbrc_genome",
                "resolution_path": "caller_supplied",
                "canonical_iri": _pre_iri,
                "canonical_label": bundle.get("resolved_species_name"),
                "canonical_ontology": "NCBITaxon",
                "confidence": 1.0,
                "resolution_status": "caller_supplied",
                "synonyms": [],
                "candidates": [],
                "needs_disambiguation": False,
                "evidence": "caller-supplied taxon_id (name resolution skipped)",
            }
        else:
            # Resolve DETERMINISTICALLY (dict only — works in desktop locus with NO server LLM).
            # Try the canonically-extracted names first, then a decomposition of the raw query:
            # this rescues an arbitrary name not in the alias table AND a combined
            # "<virus> <protein>" query. e.g. "Mayaro E1" walks ["Mayaro E1", "Mayaro E1 virus",
            # "Mayaro", "Mayaro virus"✓] and recovers protein "E1" — where the old single-term path
            # resolved the whole string as ONE (failing) entity. First EXACT, single-taxon hit wins;
            # no LLM, no fuzzy match. ``index`` is a plan placeholder — the per-index value is set by
            # the downstream map.
            candidates: list[tuple[str, str | None]] = [(n, None) for n in names]
            candidates += taxonomy_resolver.decompose_query_terms(query)
            plan = None
            recovered_protein: str | None = None
            for cand_term, cand_protein in candidates:
                try:
                    cand_plan = build_resolution_plan(
                        cand_term, index="bvbrc_genome", entity_type_str=""
                    )
                except Exception as exc:  # noqa: BLE001 — per-candidate best effort; skip + degrade
                    log.warning(
                        "EpitopeResolveStep %s: resolution probe for %r failed (%s); skipping.",
                        self.name,
                        cand_term,
                        exc,
                    )
                    continue
                # An AMBIGUOUS candidate is intentionally passed over (not a clean single-taxon
                # hit); ambiguity is surfaced only via the final-fallback re-resolve of the
                # original term below, preserving the loud ambiguous-degrade contract.
                if cand_plan.get("canonical_iri") and not cand_plan.get("needs_disambiguation"):
                    term, plan, recovered_protein = cand_term, cand_plan, cand_protein
                    break
            if plan is None:
                # No clean single-taxon hit — preserve the legacy single-term behavior (carry an
                # ambiguity / a named miss + the full raw fan-out for the unharmonized fallback).
                try:
                    plan = build_resolution_plan(term, index="bvbrc_genome", entity_type_str="")
                except Exception as exc:  # noqa: BLE001 — degrade-loud, never raise on a data miss
                    resolution_note = (
                        f"Term resolution for {term!r} (from query {query!r}) could not run "
                        f"({type(exc).__name__}: {exc}); proceeding with the unharmonized RAW "
                        f"per-index fallback across all {len(index_names)} destination indices."
                    )
                    log.warning("EpitopeResolveStep %s: %s", self.name, resolution_note)
                    plan = {
                        "term": term,
                        "index": "bvbrc_genome",
                        "resolution_path": "miss",
                        "canonical_iri": None,
                        "canonical_label": None,
                        "canonical_ontology": None,
                        "confidence": 0.0,
                        "resolution_status": "unresolved",
                        "synonyms": [],
                        "candidates": [],
                        "needs_disambiguation": False,
                        "evidence": resolution_note,
                    }
            else:
                # Clean hit. Recover the protein a combined query carried so the conservation /
                # structural / rhea legs (which read bundle['protein']) can run. If the resolving
                # candidate already dropped it (decomposition path, e.g. "Mayaro E1"), use that;
                # otherwise (resolved via an extracted alias name, e.g. "dengue NS1") find it as the
                # LONGEST query-prefix that resolves to the SAME taxon — deterministic + same-taxon
                # verified, so multi-word names are safe ("West Nile virus E" → "E", not "virus E").
                if recovered_protein is None and not bundle.get("protein"):
                    iri = plan.get("canonical_iri")
                    # The protein is the suffix AFTER the LONGEST query-prefix that resolves to the
                    # SAME taxon. Do NOT skip the full-query (None-suffix) candidate: longest-first
                    # means a bare "<X> virus" query resolves the full name FIRST → suffix None → no
                    # protein, so its trailing "virus" token is never mistaken for one.
                    for cand_term, cand_suffix in taxonomy_resolver.decompose_query_terms(query):
                        try:
                            cp = build_resolution_plan(
                                cand_term, index="bvbrc_genome", entity_type_str=""
                            )
                        except Exception:  # noqa: BLE001 — best effort; protein stays unrecovered
                            continue
                        if cp.get("canonical_iri") == iri and not cp.get("needs_disambiguation"):
                            recovered_protein = cand_suffix
                            break
                    # Defensive: a degenerate "<X> virus virus" query could leave a lone "virus"
                    # token as the suffix — that is part of the organism designation, not a protein.
                    if recovered_protein and recovered_protein.strip().lower() in {
                        "virus",
                        "viruses",
                    }:
                        recovered_protein = None
                if recovered_protein and not bundle.get("protein"):
                    bundle["protein"] = recovered_protein

        # Spread the plan fields onto the bundle (excluding ``index``).
        for key in _FLATTENED_PLAN_KEYS:
            bundle[key] = plan[key]
        bundle["resolution_plan"] = plan

        if plan["needs_disambiguation"]:
            # Ambiguous → the map no-ops; running the harmonized search across
            # heterogeneous candidate taxa would lump unrelated biology together.
            index_names = []
            bundle["_ambiguous_candidates"] = plan["candidates"]
            resolution_note = (
                f"Term {term!r} (from query {query!r}) is AMBIGUOUS — it resolves to "
                f"{len(plan['candidates'])} distinct canonical entities. The harmonized "
                f"per-index search was NOT run; re-call with a chosen canonical IRI as "
                f"the query/term to disambiguate."
            )
            log.warning("EpitopeResolveStep %s: %s", self.name, resolution_note)

        if resolution_note is not None:
            bundle["resolution_note"] = resolution_note

        bundle["index_names"] = index_names

        # Record resolution in the document's step progression (order -2 sorts it FIRST, ahead
        # of harmonized_search at -1 and the back-half stages at 0+).
        from apecx_integration.composition.steps._stage_report import append_stage_report

        _label = plan.get("canonical_label") or term
        _iri = plan.get("canonical_iri")
        resolve_md = (
            f"Resolved {term!r} → {_label!r} ({_iri}) via {plan['resolution_path']}; "
            f"fanning the search across {len(index_names)} Globus indices."
            if _iri
            else f"Resolution of {term!r}: {plan['resolution_path']} "
            f"({len(index_names)} indices). {resolution_note or ''}".strip()
        )
        append_stage_report(
            bundle,
            stage="resolve",
            order=-2,
            markdown=resolve_md,
            data={"resolution_path": plan["resolution_path"], "canonical_iri": _iri},
        )

        log.info(
            "EpitopeResolveStep %s: query=%.80r term=%r path=%s indices=%d note=%s",
            self.name,
            query,
            term,
            plan["resolution_path"],
            len(index_names),
            "set" if resolution_note else "none",
        )
        return bundle
