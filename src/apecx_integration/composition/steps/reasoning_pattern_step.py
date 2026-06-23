"""ReasoningPatternStep — embed an apecx reasoning-pattern workflow BY NAME.

A thin ``SubworkflowStep`` subclass (the framework-native "concrete subclass
customizes one classmethod" pattern) that supplies apecx's
``composition/workflows`` directory as the default search path for
``inner_workflow_name`` resolution. The consequence: a wrapper config needs only

    name: my_pattern_step
    inner_workflow_name: tdr_loop

with NO ``workflow_search_paths`` — the dir is resolved from this package's own
location (portable across installs; never an environment-specific absolute path
baked into committed YAML). This is the apecx-side adapter over nanobrain's
name-binding seam (``SubworkflowStep.inner_workflow_name``); the composer and the
pattern router reference reusable reasoning-pattern workflows (``tdr_loop``,
``best_of_n_loop``, ``rag_e2e_synthesis``, ...) through it.

nanobrain stays application-agnostic: it exposes ``_default_workflow_search_paths``
(returning ``[]``); apecx — which DOES know where its workflows live — overrides
it here. Resolution + 3-way mutual-exclusion + FAIL-LOUD-on-miss all come from
``SubworkflowStep`` unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobrain.library.steps.subworkflow_step import SubworkflowStep

# This file is .../composition/steps/reasoning_pattern_step.py, so the workflows
# dir is a sibling of ``steps`` under ``composition``. Resolved from __file__ (not
# cwd, not an absolute hardcode) so it is correct wherever apecx_integration is
# installed.
_WORKFLOWS_DIR = str(Path(__file__).resolve().parent.parent / "workflows")


class ReasoningPatternStep(SubworkflowStep):
    """SubworkflowStep that resolves ``inner_workflow_name`` against apecx's
    own ``composition/workflows`` dir by default."""

    COMPONENT_TYPE: str = "reasoning_pattern_step"

    @classmethod
    def _default_workflow_search_paths(cls) -> list[str]:
        return [_WORKFLOWS_DIR]

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Run the bound reasoning-pattern inner workflow.

        Explicit override delegating to ``SubworkflowStep.process`` so the Step
        surface declares the contract (per the nanobrain-step-authoring rule:
        a Step implements ``process``). Input/output keys are those of the bound
        inner workflow (selected by ``inner_workflow_name``) and therefore vary
        by pattern — ``SubworkflowStep`` maps the input dict onto the inner
        workflow's input data units and returns its populated output data units.
        """
        return await super().process(input_data, **kwargs)
