"""EvidenceQueryNormalizeStep — the first step of viral_epitope_evidence_review.

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
        log.info(
            "EvidenceQueryNormalizeStep %s: query=%.60r requested_outputs=%r",
            self.name,
            query,
            out.get("requested_outputs"),
        )
        return out
