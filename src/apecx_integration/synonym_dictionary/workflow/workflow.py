"""IRIResolutionWorkflow — nanobrain Workflow subclass with explicit step orchestration.

Why a Workflow subclass instead of relying on the default trigger cascade?
=========================================================================
Nanobrain's data-driven trigger cascade for inter-step links was disabled
when the framework migrated to "automatic triggers" (see
``nanobrain/core/link.py:742-773`` — ``_setup_callback_registration`` is
intentionally a no-op now), and the auto-trigger mechanism that was
supposed to replace it (``DataUnit.register_with_link``) is defined but
never invoked anywhere in the framework.  The result: ``Workflow.execute()``
populates the first step's input data unit but the second-and-later steps
never fire.

The framework's imperative fallback (``divergence_enabled: True`` →
``_process_with_divergence``) has its own bug: for single-input steps it
wraps the upstream result dict around itself before passing it down, so
``process(input_data)`` receives ``{"normalized_records": {"normalized_records": [...]}}``
instead of ``{"normalized_records": [...]}``.

We bypass both code paths and orchestrate the cascade explicitly here.
The workflow remains a real ``nanobrain.core.workflow.Workflow`` (built
via ``from_config``, owns child_steps + step_links + DataUnits), so it
can still be composed with other nanobrain workflows, but its ``process()``
walks the steps in topological order and pipes outputs to inputs without
relying on the (broken) trigger / data-unit propagation.

What this means for callers
---------------------------
- ``IRIResolutionWorkflow.from_config('iri_resolution_workflow.yml')``
  produces a usable workflow object.
- ``await workflow.process({"entity_records": [<records>]})`` returns
  ``{"resolved_records": [<enriched records>]}`` and writes the same
  list onto the workflow's ``resolved_records`` output data unit.
- The :func:`apecx_integration.synonym_dictionary.harvester_adapter.adapt_workflow_to_harvester_transform`
  helper drives this same ``process()`` for the harvester ingest stage.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.workflow import Workflow

log = logging.getLogger(__name__)


class IRIResolutionWorkflow(Workflow):
    """Two-step IRI resolution workflow with explicit step orchestration.

    Steps (topologically ordered):
        1. ``normalize`` — :class:`NormalizeEntityRecordsStep`
        2. ``resolve`` — :class:`ResolveIRIStep`

    The workflow YAML (``iri_resolution_workflow.yml``) declares both
    steps and the three DirectLinks wiring them; the links exist for
    documentation + topology validation but are not used at runtime
    because of the framework cascade issues described above.
    """

    COMPONENT_TYPE: str = "iri_resolution_workflow"

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Drive the two-step cascade explicitly.

        Parameters
        ----------
        input_data:
            Either ``{"entity_records": [<records>]}`` (canonical) or a
            raw list of records (lenient — common when the workflow's
            input data unit was set with the bare list and the framework
            wrapped it for us).

        Returns
        -------
        ``{"resolved_records": [<enriched records>]}``
        """
        entity_records = self._extract_entity_records(input_data)
        log.info(
            "IRIResolutionWorkflow %s: starting cascade with %d record(s)",
            self.name,
            len(entity_records),
        )

        normalize_step = self.child_steps.get("normalize")
        resolve_step = self.child_steps.get("resolve")
        if normalize_step is None or resolve_step is None:
            raise RuntimeError(
                f"IRIResolutionWorkflow '{self.name}': expected child_steps "
                f"'normalize' and 'resolve'; got {list(self.child_steps.keys())}"
            )

        normalize_result = await normalize_step.process({"entity_records": entity_records})
        normalized_records = normalize_result.get("normalized_records", [])
        # Mirror the framework's data-unit write so observability hooks
        # downstream still see the intermediate value.
        normalize_out_du = normalize_step.step_output_data_units.get("normalized_records")
        if normalize_out_du is not None:
            await normalize_out_du.set(normalized_records)

        resolve_result = await resolve_step.process({"normalized_records": normalized_records})
        resolved_records = resolve_result.get("resolved_records", [])
        resolve_out_du = resolve_step.step_output_data_units.get("resolved_records")
        if resolve_out_du is not None:
            await resolve_out_du.set(resolved_records)

        # Write to the workflow-level exit port too.
        workflow_out_du = self.step_output_data_units.get("resolved_records")
        if workflow_out_du is not None:
            await workflow_out_du.set(resolved_records)

        log.info(
            "IRIResolutionWorkflow %s: cascade complete; %d resolved record(s)",
            self.name,
            len(resolved_records),
        )
        return {"resolved_records": resolved_records}

    @staticmethod
    def _extract_entity_records(input_data: Any) -> list[dict[str, Any]]:
        """Pull the records list out of ``input_data``, accepting two shapes.

        Accepts:
        - ``{"entity_records": [<records>]}`` — canonical
        - ``[<records>]`` — bare list (when the data unit was set with the
          raw list and the framework hands us that directly)
        """
        if isinstance(input_data, list):
            return input_data
        if isinstance(input_data, dict):
            value = input_data.get("entity_records")
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("entity_records"), list):
                return value["entity_records"]
        raise ValueError(
            f"IRIResolutionWorkflow: cannot extract 'entity_records' from "
            f"input_data of type {type(input_data).__name__}; expected a "
            "list or a dict with 'entity_records' key."
        )
