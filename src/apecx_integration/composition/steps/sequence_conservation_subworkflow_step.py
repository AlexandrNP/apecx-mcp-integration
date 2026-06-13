"""SequenceConservationSubworkflowStep — nest viral_conserved_sites as a degrade-loud step.

A concrete ``SubworkflowStep`` (the framework's "subclass per reusable pattern" usage shape):
it embeds the lightweight ``viral_conserved_sites`` workflow (BV-BRC fetch → MAFFT MSA →
per-column conservation scoring) as a single step inside ``viral_epitope_evidence_review`` so
the evidence synthesis can reason about sequence conservation. The inner workflow comes from
the ``inner_workflow_builder`` seam (a no-arg ``build_*`` catalog callable), not a YAML path —
its ``aligner="mafft"`` arg is defaulted, so it is a valid no-arg callable. The NESTING variant
``build_viral_conserved_sites_core_workflow`` is used (not the catalog ``build_*`` one): it ends
at the ``report`` step, so the inner output is a plain ``{markdown, data:{kind:bundle, parts:
conservation_result}}`` dict — a terminal EnvelopeStep's ``WorkflowResult`` carries a ``status:
ok`` field that collides with ``SubworkflowStep``'s own ``status`` gate.

DEGRADE-LOUD (load-bearing — do not "simplify" the try/except away):
``SubworkflowStep.process`` FAIL-LOUDs on a cascade timeout, an empty inner output, <2
sequences, a MAFFT error, or a BV-BRC miss. If that exception propagated up the OUTER cascade,
the downstream fan-in (``SequenceEvidenceMergeStep``) would never receive this step's output,
the merge's ``AllDataReceivedTrigger`` would never fire, and ``Workflow.run`` would SILENTLY
return the whole evidence result empty (it swallows step exceptions — G127). So this step NEVER
raises: on any failure it returns a named ``{"sequence_conservation_unavailable": <reason>}``
marker that the merge step turns into a loud stage-report note while the rest of the evidence
still completes. It also short-circuits to that marker WITHOUT paying the inner cascade timeout
when the query carries no usable ``taxon_id`` / ``protein`` — the common evidence-question case
(those params are optional on the evidence query but REQUIRED by the conserved-sites fetch step).
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.library.steps.subworkflow_step import SubworkflowStep

log = logging.getLogger(__name__)

UNAVAILABLE_KEY = "sequence_conservation_unavailable"

_CONSERVED_SITES_BUILDER = (
    "apecx_integration.composition.workflows.viral_conserved_sites.builder"
    ".build_viral_conserved_sites_core_workflow"
)


class SequenceConservationSubworkflowStep(SubworkflowStep):
    """Embed viral_conserved_sites; degrade loud (never raise) so the evidence run completes."""

    COMPONENT_TYPE: str = "sequence_conservation_subworkflow_step"

    @classmethod
    def _default_inner_workflow_builder(cls) -> str:
        return _CONSERVED_SITES_BUILDER

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        # Fast degrade: skip the inner cascade (and its timeout) when the query has no
        # usable taxon_id/protein for the conserved-sites fetch step.
        reason = self._params_unusable(input_data)
        if reason is not None:
            log.warning(
                "SequenceConservationSubworkflowStep %s: skipping conserved-sites — %s",
                self.name,
                reason,
            )
            return {UNAVAILABLE_KEY: reason}
        try:
            return await super().process(input_data, **kwargs)
        except Exception as exc:  # noqa: BLE001 — degrade-loud is the whole point of this class
            reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "SequenceConservationSubworkflowStep %s: conserved-sites subworkflow failed — %s",
                self.name,
                reason,
            )
            return {UNAVAILABLE_KEY: reason}

    def _params_unusable(self, input_data: dict[str, Any]) -> str | None:
        """Return a loud reason string when the params can't feed conserved-sites, else None.

        Mirrors ``BvbrcProteinFastaStep``'s own validation so the pre-check matches what would
        otherwise fail (slowly, behind a timeout) inside the inner cascade.
        """
        payload = self._extract_payload(input_data)
        if not isinstance(payload, dict):
            return f"step input is not a dict ({type(payload).__name__})"
        taxon_id = payload.get("taxon_id")
        protein = payload.get("protein")
        if not (isinstance(taxon_id, int) or (isinstance(taxon_id, str) and taxon_id.isdigit())):
            return (
                "no usable NCBI taxon_id on the query — sequence conservation needs an explicit "
                f"taxon_id (resolve the virus name via harmonized_search); got {taxon_id!r}"
            )
        if not (isinstance(protein, str) and protein.strip()):
            return (
                "no protein/antigen name on the query — sequence conservation needs a protein to "
                f"fetch per-strain sequences (e.g. 'E1', 'structural polyprotein'); got {protein!r}"
            )
        return None

    def _extract_payload(self, input_data: dict[str, Any]) -> Any:
        """Unwrap the framework trigger envelope ``{<my_input_du>: payload}`` for the pre-check.

        The base ``process`` does its own routing for the real invocation; this only peeks at the
        params, so it replicates step (a) of ``_route_input_to_first_step_du`` without consuming.
        """
        my_input_dus = getattr(self, "step_input_data_units", None) or {}
        if isinstance(input_data, dict) and len(input_data) == 1:
            sole_key = next(iter(input_data))
            if sole_key in my_input_dus and isinstance(input_data[sole_key], dict):
                return input_data[sole_key]
        return input_data


__all__ = ["SequenceConservationSubworkflowStep", "UNAVAILABLE_KEY"]
