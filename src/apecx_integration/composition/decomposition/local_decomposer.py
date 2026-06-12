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
from typing import Any, Protocol

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
        mode: str = "auto_solver",
        inputs_resolver: Any = None,
    ) -> None:
        self._matcher = matcher
        self._decomposer = decomposer
        self._dispatcher = dispatcher
        self._max_depth = max_depth
        self._max_dispatches = max_dispatches
        self._match_threshold = match_threshold
        self._dispatch_count = 0
        # RoC-3 — mode: "auto_solver" (default; bounded autonomous solving) or "plan_returner"
        # (return of control: propose a plan as needs_input(decomposition_choice)).
        self._mode = mode
        # name -> {required, properties, obtain_via} (RoC-2b derive_required_inputs); lets the
        # plan name each workflow's required params. None → plan lists workflows without params.
        self._inputs_resolver = inputs_resolver

    async def solve(self, task: Task, *, _depth: int = 0) -> WorkflowResult:
        # RoC-3b — plan_returner returns control to the frontier LLM at the planning stage; it does
        # NOT execute. (Only at the top level; nested auto_solver recursion is unaffected.)
        if self._mode == "plan_returner" and _depth == 0:
            return await self._plan(task)
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

    async def _plan(self, task: Task) -> WorkflowResult:
        """plan_returner — match (+ decompose if needed) and return a decomposition_choice control
        transfer naming the workflow(s) + their required inputs, for the frontier LLM to run."""
        from apecx_integration.composition.schemas.control_transfer import (
            WorkflowNeed,
            decomposition_choice_transfer,
        )

        match = await self._matcher.match(task)
        if match is not None and match.score >= self._match_threshold:
            needs = [self._workflow_need(match.workflow_name, task)]
        else:
            subtasks = await self._decomposer.decompose(task)
            if not subtasks:
                # Loud "cannot solve" — no fabricated plan.
                return WorkflowResult.failed(
                    f"no workflow matches {task.description!r} and it is not decomposable."
                )
            needs = []
            for st in subtasks:
                m = await self._matcher.match(st)
                if m is not None and m.score >= self._match_threshold:
                    needs.append(self._workflow_need(m.workflow_name, st))
                else:
                    # Honest: a subtask with no matching workflow is named, not silently dropped.
                    needs.append(WorkflowNeed(workflow=f"(no workflow matches: {st.description})"))
        return WorkflowResult.needs_input(
            decomposition_choice_transfer(needs),
            markdown=f"Proposed plan for: {task.description}",
        )

    def _workflow_need(self, name: str, task: Task) -> Any:
        from apecx_integration.composition.schemas.control_transfer import WorkflowNeed

        derived = self._inputs_resolver(name) if self._inputs_resolver else {}
        required = list(derived.get("required", []) if isinstance(derived, dict) else [])
        payload = task.payload or {}
        provided = [k for k in required if k in payload]
        missing = [k for k in required if k not in payload]
        return WorkflowNeed(
            workflow=name, required_inputs=required, provided=provided, missing=missing
        )

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
