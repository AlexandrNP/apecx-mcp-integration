"""ProteinNameNormalizationSubworkflowStep — nest protein_name_normalization as a passthrough-degrade step.

A concrete ``SubworkflowStep`` that embeds the lightweight ``protein_name_normalization`` workflow
(BV-BRC product-catalog lookup → token-subset match) as a single step at the FRONT of the
conserved-sites cascade, so the sequence fetch receives the taxon's actual BV-BRC product term
instead of a name that fails the substring filter. The inner workflow comes from the
``inner_workflow_builder`` seam (the no-arg ``build_protein_name_normalization_core_workflow``), the
NESTING variant that ends at the ``normalize`` step — its plain ``{taxon_id, protein, feature_type,
...}`` dict composes cleanly inside a ``SubworkflowStep`` (a terminal EnvelopeStep's
``WorkflowResult.status: 'ok'`` collides with ``SubworkflowStep``'s own ``status`` gate).

DEGRADE-TO-ORIGINAL (load-bearing — do not "simplify" the try/except away): normalization is an
ENHANCEMENT, not a gate. ``SubworkflowStep.process`` FAIL-LOUDs on a cascade timeout, an empty inner
output, or a framework error. If that propagated, the whole conserved-sites cascade (and the epitope
analysis nesting it) would abort. So on ANY inner failure this step returns the ORIGINAL, un-normalized
payload — the downstream ``BvbrcProteinFastaStep`` then runs with the user's literal name, i.e.
EXACTLY the behavior it would have without this step. (Contrast ``SequenceConservationSubworkflowStep``,
whose failure is genuine evidence-absence and emits an "unavailable" marker; here the failure must be
invisible.) The inner step itself never raises — it passes through internally on any BV-BRC error — so
this wrapper's catch only fires on framework-level cascade failures.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.library.steps.subworkflow_step import SubworkflowStep

log = logging.getLogger(__name__)

_NORMALIZATION_BUILDER = (
    "apecx_integration.composition.workflows.protein_name_normalization.builder"
    ".build_protein_name_normalization_core_workflow"
)


class ProteinNameNormalizationSubworkflowStep(SubworkflowStep):
    """Embed protein_name_normalization; degrade to the ORIGINAL payload (never raise)."""

    COMPONENT_TYPE: str = "protein_name_normalization_subworkflow_step"

    @classmethod
    def _default_inner_workflow_builder(cls) -> str:
        return _NORMALIZATION_BUILDER

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            return await super().process(input_data, **kwargs)
        except Exception as exc:  # noqa: BLE001 — normalization is an enhancement; failure must be invisible
            reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "ProteinNameNormalizationSubworkflowStep %s: normalization subworkflow failed (%s); "
                "passing the ORIGINAL protein name through unchanged",
                self.name,
                reason,
            )
            payload = self._extract_payload(input_data)
            out = dict(payload) if isinstance(payload, dict) else {}
            out.setdefault("original_protein", out.get("protein"))
            out["match_source"] = "passthrough_error"
            return out

    def _extract_payload(self, input_data: dict[str, Any]) -> Any:
        """Unwrap the framework trigger envelope ``{<my_input_du>: payload}`` to recover the original
        params for the degrade path. Mirrors step (a) of ``_route_input_to_first_step_du`` without
        consuming (the base ``process`` does its own routing for the real invocation)."""
        my_input_dus = getattr(self, "step_input_data_units", None) or {}
        if isinstance(input_data, dict) and len(input_data) == 1:
            sole_key = next(iter(input_data))
            if sole_key in my_input_dus and isinstance(input_data[sole_key], dict):
                return input_data[sole_key]
        return input_data


__all__ = ["ProteinNameNormalizationSubworkflowStep"]
