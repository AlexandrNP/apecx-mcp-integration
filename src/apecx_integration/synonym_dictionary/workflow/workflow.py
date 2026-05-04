"""IRIResolutionWorkflow — nanobrain Workflow subclass with synchronous orchestration.

Two equally-valid invocation paths
==================================
Both work correctly as of 2026-05-04 (after the
``nanobrain/core/link.py::_setup_callback_registration`` fix that
re-enabled inter-step DirectLink change listeners):

1. **Synchronous, single-call** (this class's ``process()``):
   ``await workflow.process({"entity_records": [<records>]})`` walks
   the steps in topological order, pipes outputs to inputs, and returns
   ``{"resolved_records": [<enriched records>]}`` once the whole cascade
   has completed. The harvester adapter uses this path because it needs
   a deterministic per-record return value.

2. **Data-driven, event-fired** (the framework's default cascade):
   Setting the workflow input data unit and calling
   ``await workflow.execute()`` kicks off the cascade via DataUnit
   change listeners. Returns control as soon as the first step is
   triggered; the rest of the chain runs in the background. The
   workflow output data unit is populated when the cascade completes.
   Tests that exercise this path appear in
   ``tests/integration/test_iri_resolution_workflow.py::
   test_workflow_native_framework_cascade``.

Why a Workflow subclass at all?
-------------------------------
The synchronous ``process()`` method on this subclass is the canonical
in-process API for the harvester adapter and any future caller that
wants to await a complete result without writing
``await asyncio.sleep(...)`` to let the async cascade finish. The base
``Workflow.process()`` runs in data-driven mode and returns
``{"status": "data_flow_initiated", ...}`` rather than the resolved
records — useful for monitoring, awkward for one-shot lookups.

Both paths use the same step instances, the same data units, and the
same framework plumbing. The choice is just about whether the caller
wants the answer in the return value or via an out-of-band data unit
read.
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
