"""Workflow matchers for local decomposition (EO-20 matcher impl).

``KeywordWorkflowMatcher`` scores a task against a catalog of workflow descriptions by Jaccard
token overlap — a deterministic, infra-free baseline. The richer impl is semantic RAG over
workflow descriptions/examples; it implements the same ``WorkflowMatcher`` protocol and swaps in
without touching ``LocalDecomposer``. The baseline exists so the decomposition path is testable
and usable before the RAG index is built (and as a fallback when it is absent).
"""

from __future__ import annotations

import re

from apecx_integration.composition.decomposition.local_decomposer import MatchResult, Task

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


class KeywordWorkflowMatcher:
    """Jaccard token-overlap matcher over a ``{workflow_name: description}`` catalog."""

    def __init__(self, catalog: dict[str, str]) -> None:
        self._catalog = {name: _tokenize(desc) for name, desc in catalog.items()}

    async def match(self, task: Task) -> MatchResult | None:
        task_tokens = _tokenize(task.description)
        if not task_tokens:
            return None
        best_name: str | None = None
        best_score = 0.0
        for name, keywords in self._catalog.items():
            union = task_tokens | keywords
            if not union:
                continue
            score = len(task_tokens & keywords) / len(union)
            if score > best_score:
                best_name, best_score = name, score
        if best_name is None:
            return None
        return MatchResult(best_name, best_score)
