"""Local bounded decomposition (EO-20) — the local-LLM discretion layer.

When a (sub)task is handed to a workflow's local reasoning, the policy is **match a single
workflow first**; only if no workflow matches AND the task is decomposable does the local LLM
decompose it into sub-workflows and dispatch each (recursively). Determinism is the default
path; LLM discretion is the bounded exception.

This module is the deterministic CONTROL STRUCTURE. The LLM/RAG/runtime parts are injected as
async protocols:
  - ``WorkflowMatcher.match`` — RAG/lookup over the workflow catalog (real impl); returns a
    match above threshold or None.
  - ``TaskDecomposer.decompose`` — the local LLM (real impl); returns sub-tasks, or ``[]`` when
    the task cannot be decomposed.
  - ``WorkflowDispatcher.dispatch`` — runs a matched workflow (real impl: ``Workflow.run`` via
    ``run_workflow_observed``), returns its ``WorkflowResult``.

Bounds (loud, never silent): ``max_depth`` (recursion) and ``max_dispatches`` (total fan-out)
are plain counters — the honest bounds for recursion + fan-out (``LoopController`` is for
workflow back-edges, not recursion; ``CostEnvelope`` enforces usd/tokens/walltime, not dispatch
counts). When a bound is hit, or nothing matches and nothing decomposes, the result is a
loud ``WorkflowResult`` with ``status='error'`` and a message — never a silent empty answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from apecx_integration.composition.schemas.workflow_result import WorkflowResult


@dataclass
class Task:
    description: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    workflow_name: str
    score: float


class WorkflowMatcher(Protocol):
    async def match(self, task: Task) -> MatchResult | None: ...


class TaskDecomposer(Protocol):
    async def decompose(self, task: Task) -> list[Task]: ...


class WorkflowDispatcher(Protocol):
    async def dispatch(self, workflow_name: str, task: Task) -> WorkflowResult: ...


class LocalDecomposer:
    def __init__(
        self,
        matcher: WorkflowMatcher,
        decomposer: TaskDecomposer,
        dispatcher: WorkflowDispatcher,
        *,
        max_depth: int = 3,
        max_dispatches: int = 20,
        match_threshold: float = 0.0,
    ) -> None:
        self._matcher = matcher
        self._decomposer = decomposer
        self._dispatcher = dispatcher
        self._max_depth = max_depth
        self._max_dispatches = max_dispatches
        self._match_threshold = match_threshold
        self._dispatch_count = 0

    async def solve(self, task: Task, *, _depth: int = 0) -> WorkflowResult:
        if _depth > self._max_depth:
            return WorkflowResult.failed(
                f"max_depth {self._max_depth} exceeded while decomposing "
                f"{task.description!r} — cannot solve without unbounded recursion."
            )

        match = await self._matcher.match(task)
        if match is not None and match.score >= self._match_threshold:
            if self._dispatch_count >= self._max_dispatches:
                return WorkflowResult.failed(
                    f"max_dispatches {self._max_dispatches} exceeded — refusing to dispatch "
                    f"{match.workflow_name!r} for {task.description!r}."
                )
            self._dispatch_count += 1
            return await self._dispatcher.dispatch(match.workflow_name, task)

        subtasks = await self._decomposer.decompose(task)
        if not subtasks:
            # No single workflow matched AND the task is not decomposable — loud, not a
            # fabricated empty answer. This is the first-class "cannot solve" output.
            return WorkflowResult.failed(
                f"no workflow matches {task.description!r} and it is not decomposable."
            )

        results = [await self.solve(st, _depth=_depth + 1) for st in subtasks]
        return self._integrate(task, subtasks, results)

    def _integrate(
        self, task: Task, subtasks: list[Task], results: list[WorkflowResult]
    ) -> WorkflowResult:
        parts: list[str] = []
        degraded = False
        for st, r in zip(subtasks, results, strict=True):
            if r.status == "error":
                # Surface the child's error message into the aggregate. A failed child has
                # empty markdown (its message lives in .error); dropping it here would make
                # the aggregate "partial" with no visible reason — a silent-failure shape.
                body = f"**ERROR:** {r.error or 'unknown error'}"
                degraded = True
            else:
                body = r.markdown
                if r.status == "partial":
                    degraded = True
            parts.append(f"### {st.description}\n\n{body}")
        markdown = f"## {task.description}\n\n" + "\n\n".join(parts)
        # Any child error OR partial makes the aggregate PARTIAL (never silently "ok") so the
        # orchestrating LLM knows some branch did not fully complete.
        return WorkflowResult(markdown=markdown, status="partial" if degraded else "ok")
