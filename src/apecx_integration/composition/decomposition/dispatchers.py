"""Workflow dispatcher for local decomposition (EO-20 dispatcher impl).

``RunWorkflowDispatcher`` resolves a matched workflow by name and runs it via
``run_workflow_observed`` (real ``Workflow.run`` + provenance), returning its ``WorkflowResult``.
Loud on both failure modes: an unknown workflow name (the loader raises and it propagates) and a
workflow that runs but emits no envelope (a ``WorkflowResult.failed``, never a silent empty one).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apecx_integration.composition.decomposition.local_decomposer import Task
from apecx_integration.composition.runtime.observed_run import run_workflow_observed
from apecx_integration.composition.schemas.workflow_result import WorkflowResult


class RunWorkflowDispatcher:
    """Dispatch a matched workflow by name. ``workflow_loader(name)`` returns a runnable
    ``Workflow`` (or raises loudly on an unknown name)."""

    def __init__(
        self,
        workflow_loader: Callable[[str], Any],
        *,
        input_envelope_resolver: Callable[[str], str | None] | None = None,
        **run_kwargs: Any,
    ) -> None:
        self._loader = workflow_loader
        # Resolve the catalog's input_envelope_key per workflow so a structured task.payload is
        # deposited where Workflow.run expects it (the FIRST-STEP input-DU name). Without this,
        # run(payload) deposits nothing for a workflow with a named entry port — a silent empty
        # run. None (default) preserves the legacy raw-payload behavior.
        self._envelope_resolver = input_envelope_resolver
        self._run_kwargs = run_kwargs

    async def dispatch(self, workflow_name: str, task: Task) -> WorkflowResult:
        workflow = self._loader(workflow_name)  # loud on unknown name
        input_data = task.payload
        if self._envelope_resolver is not None:
            key = self._envelope_resolver(workflow_name)
            if key is not None:
                input_data = {key: task.payload}
        outcome = await run_workflow_observed(workflow, input_data, **self._run_kwargs)
        if outcome.workflow_result is not None:
            return outcome.workflow_result
        return WorkflowResult.failed(
            f"workflow {workflow_name!r} ran but emitted no WorkflowResult envelope "
            f"(status={outcome.raw_result.get('status')!r})"
        )
