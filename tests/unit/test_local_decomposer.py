"""Unit tests for the local bounded decomposition control structure (EO-20).

Deterministic fakes for matcher/decomposer/dispatcher — the control logic (match-first,
bounded recursion, loud cannot-solve) is proven without any LLM.
"""

from __future__ import annotations

import asyncio

from apecx_integration.composition.decomposition.local_decomposer import (
    LocalDecomposer,
    MatchResult,
    Task,
)
from apecx_integration.composition.schemas.workflow_result import WorkflowResult


class FakeMatcher:
    def __init__(self, matches: dict[str, MatchResult]):
        self._m = matches

    async def match(self, task: Task) -> MatchResult | None:
        return self._m.get(task.description)


class FakeDecomposer:
    def __init__(self, decomp: dict[str, list[str]]):
        self._d = decomp

    async def decompose(self, task: Task) -> list[Task]:
        return [Task(d) for d in self._d.get(task.description, [])]


class FakeDispatcher:
    def __init__(self):
        self.dispatched: list[str] = []

    async def dispatch(self, workflow_name: str, task: Task) -> WorkflowResult:
        self.dispatched.append(workflow_name)
        return WorkflowResult(markdown=f"ran {workflow_name} for {task.description}")


def _decomposer(matches=None, decomp=None, dispatcher=None, **kw) -> LocalDecomposer:
    return LocalDecomposer(
        FakeMatcher(matches or {}),
        FakeDecomposer(decomp or {}),
        dispatcher or FakeDispatcher(),
        **kw,
    )


def test_match_first_dispatches_single_workflow():
    disp = FakeDispatcher()
    d = _decomposer(matches={"A": MatchResult("wfA", 1.0)}, dispatcher=disp)
    r = asyncio.run(d.solve(Task("A")))
    assert r.status == "ok"
    assert disp.dispatched == ["wfA"]
    assert "ran wfA" in r.markdown


def test_decompose_when_no_single_match():
    disp = FakeDispatcher()
    d = _decomposer(
        matches={"B1": MatchResult("wfB1", 1.0), "B2": MatchResult("wfB2", 1.0)},
        decomp={"B": ["B1", "B2"]},
        dispatcher=disp,
    )
    r = asyncio.run(d.solve(Task("B")))
    assert r.status == "ok"
    assert disp.dispatched == ["wfB1", "wfB2"]
    assert "ran wfB1" in r.markdown and "ran wfB2" in r.markdown


def test_cannot_solve_is_loud():
    d = _decomposer(matches={}, decomp={})
    r = asyncio.run(d.solve(Task("Z")))
    assert r.status == "error"
    assert "not decomposable" in (r.error or "")


def test_depth_cap_halts_loudly():
    # No match ever; decomposes into one child forever -> would recurse without bound.
    d = _decomposer(matches={}, decomp={"X": ["X"]}, max_depth=2)
    r = asyncio.run(d.solve(Task("X")))
    assert r.status in {"error", "partial"}
    # The deepest node hits the depth cap with a loud error somewhere in the tree.
    assert "max_depth" in r.markdown or "max_depth" in (r.error or "")


def test_max_dispatches_budget_enforced():
    disp = FakeDispatcher()
    d = _decomposer(
        matches={k: MatchResult(f"wf{k}", 1.0) for k in ["C1", "C2", "C3"]},
        decomp={"C": ["C1", "C2", "C3"]},
        dispatcher=disp,
        max_dispatches=2,
    )
    r = asyncio.run(d.solve(Task("C")))
    # Only 2 dispatches allowed; the 3rd is refused loudly -> aggregate is partial.
    assert len(disp.dispatched) == 2
    assert r.status == "partial"
    assert "max_dispatches" in r.markdown


def test_partial_when_a_child_cannot_solve():
    disp = FakeDispatcher()
    d = _decomposer(
        matches={"D1": MatchResult("wfD1", 1.0)},  # D2 has no match + no decomp
        decomp={"D": ["D1", "D2"]},
        dispatcher=disp,
    )
    r = asyncio.run(d.solve(Task("D")))
    assert r.status == "partial"
    assert "ran wfD1" in r.markdown
    assert "not decomposable" in r.markdown  # D2's loud failure surfaced in the aggregate


def test_match_below_threshold_falls_through_to_decompose():
    disp = FakeDispatcher()
    d = _decomposer(
        matches={"E": MatchResult("wfE_weak", 0.3), "E1": MatchResult("wfE1", 0.9)},
        decomp={"E": ["E1"]},
        dispatcher=disp,
        match_threshold=0.8,
    )
    r = asyncio.run(d.solve(Task("E")))
    # The weak 0.3 match for E is below 0.8 -> decompose; E1's 0.9 match dispatches.
    assert disp.dispatched == ["wfE1"]
    assert r.status == "ok"
