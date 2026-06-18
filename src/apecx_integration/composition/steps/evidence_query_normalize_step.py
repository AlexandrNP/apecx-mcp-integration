"""EvidenceQueryNormalizeStep — the PARSE-ONLY entry step of viral_epitope_analysis.

It is the deposit point that captures the workflow's input params and feeds them into the
resolution chain (``resolve`` → synonym_gen → bvbrc_search → taxon_review). It does NOT resolve a
taxon itself — resolution lives ONCE in that chain — and it does NOT fan out to the gate / sequence
leg directly; the chain's terminal step (``taxon_review``) fans the fully-resolved bundle out.

WHY THIS STEP EXISTS (load-bearing): ``run_workflow`` deposits the input under the catalog's
``input_envelope_key`` = THIS step's input DU (``normalize_input``), NOT the workflow-level
``workflow_input`` DU. So the params must be captured here and threaded forward from THIS step's
real OUTPUT DU; downstream the resolution chain fans the resolved bundle (incl. the control fields
``requested_outputs`` / ``design_approval_id``) to the harmonized search, the sequence leg, and
``gate.control_in``.

Input contract (after the framework ``{normalize_input: payload}`` unwrap):
``{query, taxon_id?, protein?, requested_outputs?, design_approval_id?}``.
Output: the same dict, plus — for a caller-supplied ``taxon_id`` — a seeded ``canonical_iri`` so
the resolution chain short-circuits and honors it (the dict + LLM fallback are skipped).
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
        # Parse-only passthrough: one output DU feeds resolve (which runs the dict resolver) and,
        # downstream of the resolve->fallback chain, the sequence leg + gate. Taxon RESOLUTION is
        # NO LONGER done here — it lives once in `resolve` (dict) + the LLM-fallback chain, so the
        # sequence leg consumes the SAME resolved taxon as the harmonized search (single source).
        out = dict(input_data)
        # Honor a caller-supplied taxon_id: seed canonical_iri so the resolve->fallback chain
        # short-circuits on it (the resolve step skips when canonical_iri is already set).
        supplied = out.get("taxon_id")
        if isinstance(supplied, int) or (isinstance(supplied, str) and supplied.strip().isdigit()):
            tid = int(supplied)
            out["taxon_id"] = tid
            out["canonical_iri"] = f"http://purl.obolibrary.org/obo/NCBITaxon_{tid}"
        log.info(
            "EvidenceQueryNormalizeStep %s: query=%.60r taxon_id=%r requested_outputs=%r",
            self.name,
            query,
            out.get("taxon_id"),
            out.get("requested_outputs"),
        )
        return out
