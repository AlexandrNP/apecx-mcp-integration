"""Local bounded task decomposition (EO-20): match-a-workflow-first, else decompose."""

from apecx_integration.composition.decomposition.dispatchers import RunWorkflowDispatcher
from apecx_integration.composition.decomposition.llm_decomposer import LLMTaskDecomposer
from apecx_integration.composition.decomposition.local_decomposer import (
    LocalDecomposer,
    MatchResult,
    Task,
    TaskDecomposer,
    WorkflowDispatcher,
    WorkflowMatcher,
)
from apecx_integration.composition.decomposition.matchers import KeywordWorkflowMatcher

__all__ = [
    "LocalDecomposer",
    "Task",
    "MatchResult",
    "WorkflowMatcher",
    "TaskDecomposer",
    "WorkflowDispatcher",
    "KeywordWorkflowMatcher",
    "RunWorkflowDispatcher",
    "LLMTaskDecomposer",
]
