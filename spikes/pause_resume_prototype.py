"""T00.2 spike — pause/resume prototype against nanobrain LocalExecutor.

Question: does nanobrain's executor model support a step whose ``process()``
suspends pending an external decision (an HTTP callback) and resumes cleanly?

This prototype answers YES for the single-process, single-host case against
``LocalExecutor``. It documents the caveats that shape the real ``ApprovalStep``
(T10) design.

Run:
    python spikes/pause_resume_prototype.py

Expected output: two successful scenarios and one timeout-induced failure that
demonstrates the pause really is a pause.

Caveats surfaced (see docs/spikes/async_pause_resume.md for the full verdict):
- The pause state is an in-process ``asyncio.Event``. If the Python process
  dies, the pause is lost. Real T10 must poll the Control Plane over HTTP so
  restart recovery works.
- ``LocalExecutor`` acquires a semaphore bounded by ``max_workers`` (default 5)
  for every task; N concurrent paused steps deadlocks the N+1th step.
- This spike uses direct instantiation of a simple Event-aware coroutine, not
  a full ``from_config`` ``Step`` subclass. The executor's role is the same
  (it just awaits the coroutine); the ``from_config`` scaffolding is orthogonal
  to the question being answered.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 2026-04-21 update: nanobrain's packaging was fixed (scope-decision memo 03),
# so `pip install -e ../nanobrain` works. The sys.path fallback below remains as
# a safety net for users who haven't installed nanobrain yet; real integration
# code should rely on the pip install.
NANOBRAIN_SRC = Path(__file__).resolve().parent.parent.parent / "nanobrain"
if str(NANOBRAIN_SRC) not in sys.path:
    sys.path.insert(0, str(NANOBRAIN_SRC))


@dataclass
class SynonymProposal:
    entity: str
    candidates: list[tuple[str, float]]


@dataclass
class ApprovalDecision:
    status: str  # "approved" | "rejected" | "approved_with_modifications"
    modifications: dict[str, str] | None = None
    comment: str = ""


class InMemoryApprovalStore:
    """Stand-in for what the Control Plane (Tier 2) will hold durably in T09.

    The spike uses this in-memory store to decouple the simulated workflow
    coroutine from the simulated MCP decision call. The real implementation
    persists pending approvals to SQLite, and the step polls via HTTP.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._decisions: dict[str, ApprovalDecision] = {}

    def register_pending(self, approval_id: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._events[approval_id] = ev
        return ev

    def record_decision(self, approval_id: str, decision: ApprovalDecision) -> None:
        self._decisions[approval_id] = decision
        self._events[approval_id].set()

    def decision_for(self, approval_id: str) -> ApprovalDecision:
        return self._decisions[approval_id]


async def approval_step_process(
    store: InMemoryApprovalStore,
    approval_id: str,
    proposals: list[SynonymProposal],
    timeout_seconds: float | None = None,
) -> ApprovalDecision:
    """Simulates what the T10 ``ApprovalStep.process()`` will do.

    Real T10 will POST to ``/approvals/`` on the Control Plane, then long-poll
    or subscribe to SSE for the decision. This spike uses an ``asyncio.Event``
    to stand in for that network roundtrip.
    """
    ev = store.register_pending(approval_id)
    print(f"  [step] registered pending approval {approval_id} with {len(proposals)} proposals")

    if timeout_seconds is None:
        await ev.wait()
    else:
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            print(f"  [step] timed out after {timeout_seconds}s")
            raise

    decision = store.decision_for(approval_id)
    print(f"  [step] resumed with decision: {decision.status}")
    return decision


async def mcp_surface_approve_after_delay(
    store: InMemoryApprovalStore,
    approval_id: str,
    delay_seconds: float,
    decision: ApprovalDecision,
) -> None:
    """Stands in for the MCP surface telling the Control Plane "user approved"."""
    await asyncio.sleep(delay_seconds)
    print(
        f"  [mcp] user decided {decision.status} for {approval_id} "
        f"after {delay_seconds}s"
    )
    store.record_decision(approval_id, decision)


CONFIG_PATH = Path(__file__).resolve().parent / "configs" / "local_executor.yml"


def make_local_executor() -> object:
    """Instantiate a LocalExecutor via the sanctioned ``from_config`` path.

    Returns ``None`` if nanobrain cannot be imported (e.g., missing deps). In
    that case the spike falls back to raw ``asyncio.create_task`` — which is
    exactly what LocalExecutor does internally. The fallback still answers the
    core question ("does the async pattern work") while being honest that the
    real executor code path was not exercised.
    """
    try:
        from nanobrain.core.executor import LocalExecutor
    except Exception as exc:  # noqa: BLE001 — spike code, want the full signal
        print(f"  [spike] nanobrain import failed: {exc}")
        return None

    return LocalExecutor.from_config(str(CONFIG_PATH))


async def run_against_local_executor(
    scenario: str,
    task_factory,  # type: ignore[no-untyped-def]
) -> None:
    """Run a coroutine through LocalExecutor.execute and time it.

    Fallback: if nanobrain can't be imported (dep or packaging issue), run the
    same coroutine directly via ``asyncio.create_task`` — which is exactly what
    ``LocalExecutor.execute`` does internally. We annotate the output so the
    reader knows which path was exercised.
    """
    executor = make_local_executor()
    start = time.perf_counter()
    if executor is None:
        print(f"  [{scenario}] running via fallback asyncio.create_task (nanobrain unavailable)")
        task = asyncio.create_task(task_factory())
        result = await task
    else:
        await executor.initialize()  # type: ignore[attr-defined]
        print(f"  [{scenario}] running via nanobrain LocalExecutor")
        result = await executor.execute(task_factory)  # type: ignore[attr-defined]
    elapsed = time.perf_counter() - start
    print(f"  [{scenario}] result={result} elapsed={elapsed:.2f}s")


async def scenario_approved() -> None:
    print("\n=== Scenario 1: user approves after 0.5s ===")
    store = InMemoryApprovalStore()
    approval_id = "spike-01"
    proposals = [
        SynonymProposal(
            entity="pathogen:chikungunya",
            candidates=[("Chikungunya virus", 0.98), ("CHIKV", 0.85)],
        )
    ]

    async def paused_task() -> ApprovalDecision:
        return await approval_step_process(store, approval_id, proposals)

    setter = asyncio.create_task(
        mcp_surface_approve_after_delay(
            store,
            approval_id,
            delay_seconds=0.5,
            decision=ApprovalDecision(status="approved"),
        )
    )
    await run_against_local_executor("scenario_approved", paused_task)
    await setter


async def scenario_corrected() -> None:
    print("\n=== Scenario 2: user corrects after 0.2s ===")
    store = InMemoryApprovalStore()
    approval_id = "spike-02"
    proposals = [
        SynonymProposal(
            entity="vaccine:vaccines",
            candidates=[("CHIKV vaccine X", 0.76), ("Attenuated Y", 0.62)],
        )
    ]

    async def paused_task() -> ApprovalDecision:
        return await approval_step_process(store, approval_id, proposals)

    setter = asyncio.create_task(
        mcp_surface_approve_after_delay(
            store,
            approval_id,
            delay_seconds=0.2,
            decision=ApprovalDecision(
                status="approved_with_modifications",
                modifications={"vaccine:vaccines": "CHIKV vaccine Z"},
                comment="Preferring the more recent candidate",
            ),
        )
    )
    await run_against_local_executor("scenario_corrected", paused_task)
    await setter


async def scenario_timeout() -> None:
    print("\n=== Scenario 3: soft-gate timeout (0.3s) with no decision ===")
    store = InMemoryApprovalStore()
    approval_id = "spike-03"
    proposals = [
        SynonymProposal(entity="gene:E1", candidates=[("E1 glycoprotein", 0.91)])
    ]

    async def paused_task() -> str:
        try:
            await approval_step_process(
                store, approval_id, proposals, timeout_seconds=0.3
            )
        except asyncio.TimeoutError:
            return "timed_out_as_expected"
        return "unexpected_no_timeout"

    await run_against_local_executor("scenario_timeout", paused_task)


async def main() -> None:
    print("T00.2 spike — nanobrain LocalExecutor pause/resume")
    print("=" * 60)
    await scenario_approved()
    await scenario_corrected()
    await scenario_timeout()
    print("\nAll scenarios completed. See docs/spikes/async_pause_resume.md for the verdict.")


if __name__ == "__main__":
    asyncio.run(main())
