"""Static workflow inspection — recursive YAML-tree resolution (EO-02)."""

from apecx_integration.composition.inspection.workflow_inspector import (
    LinkInspection,
    StepInspection,
    WorkflowInspection,
    inspect_workflow,
)

__all__ = [
    "inspect_workflow",
    "WorkflowInspection",
    "StepInspection",
    "LinkInspection",
]
