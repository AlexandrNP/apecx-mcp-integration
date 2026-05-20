"""Local bounded task decomposition (EO-20): match-a-workflow-first, else decompose."""

from apecx_integration.composition.decomposition.local_decomposer import (
    LocalDecomposer,
    MatchResult,
    Task,
    TaskDecomposer,
    WorkflowDispatcher,
    WorkflowMatcher,
)

__all__ = [
    "LocalDecomposer",
    "Task",
    "MatchResult",
    "WorkflowMatcher",
    "TaskDecomposer",
    "WorkflowDispatcher",
]
