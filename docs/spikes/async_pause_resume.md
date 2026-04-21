# T00.2 Spike Verdict — nanobrain async pause/resume

**Date:** 2026-04-21
**Question (from implementation_plan.md T00.2):** Does nanobrain's executor model support a step whose ``process()`` suspends pending an external decision and resumes cleanly, without modifying nanobrain core?
**Verdict:** **YES, with caveats.** No nanobrain core changes required for single-process, single-host operation. Real `ApprovalStep` (T10) must account for three design implications noted in §3.

---

## 1. Prototype

`spikes/pause_resume_prototype.py` + `spikes/configs/local_executor.yml`.

Three scenarios:

| # | Scenario | Expected wall-time | Actual |
|---|---|---|---|
| 1 | User approves after 0.5s | 0.5s | **0.50s** |
| 2 | User corrects (approved_with_modifications) after 0.2s | 0.2s | **0.20s** |
| 3 | Soft-gate timeout: user never decides, step times out at 0.3s | 0.3s | **0.30s** |

All ran against a real `LocalExecutor` loaded via `LocalExecutor.from_config('spikes/configs/local_executor.yml')`.

### Command to reproduce

```bash
cd apecx-mcp-integration
./.venv/bin/pip install aiofiles  # nanobrain dep, not listed in its pyproject
./.venv/bin/python spikes/pause_resume_prototype.py
```

### What the spike does

`approval_step_process(store, approval_id, proposals)`:
1. Registers an `asyncio.Event` against an `approval_id` in an in-memory `InMemoryApprovalStore`.
2. Awaits the event.
3. A separate coroutine (`mcp_surface_approve_after_delay`) — standing in for the MCP surface telling the Control Plane "user approved" — sleeps N seconds and then calls `store.record_decision(approval_id, decision)`, which sets the event.
4. The suspended `process()` resumes and returns the decision.

The coroutine is dispatched through `LocalExecutor.execute(task_factory)`, which is exactly what `Step.execute()` does at `nanobrain/nanobrain/core/step.py:1405`.

---

## 2. Why it works (cheap version of the proof)

From `step.py:1358-1411` and `executor.py:573-611`:

1. `Step.execute()` calls `self.executor.execute(execute_wrapper)` where `execute_wrapper` is an async closure that calls `self._execute_process(input_data, **kwargs)` (which calls `process`).
2. `LocalExecutor.execute(task)` acquires `self._semaphore` (bounded by `max_workers`) and then either:
   - If `task` is a coroutine: `await task`.
   - If `task` is an async function: `asyncio.create_task(task(**kwargs))` then `await async_task`.
3. Neither path imposes a timeout. If the coroutine awaits an `asyncio.Event`, the executor just keeps waiting.
4. The semaphore is released when the coroutine returns. Pausing does not release the semaphore slot.

The pattern we need — "`process()` awaits something external" — is a pure `asyncio` capability that `LocalExecutor` does not get in the way of.

---

## 3. The three caveats that shape T10

### 3.1 Durability: in-process Event is NOT what T10 should use

In the spike, the `asyncio.Event` lives in the Python process. If the process dies during a pause, the pause is gone; there is nothing to resume.

**Design implication for T10:** the real `ApprovalStep` must NOT rely on an in-process Event. It should:

- POST to the Control Plane (`POST /approvals/`) to create a durable pending-approval row in the DB.
- Long-poll (or subscribe via SSE) on the Control Plane for the decision.
- On process restart, the Control Plane looks up pending approvals and — at the moment a new `ApprovalStep.process()` is entered for a resumed run — returns the existing decision (if one was recorded while the process was down) or re-establishes the wait.

The spike's `InMemoryApprovalStore` is the *shape* of what the Control Plane (Tier 2) does. It is not something that goes into production code.

### 3.2 Semaphore: max_workers is the concurrent-pause ceiling

`LocalExecutor` holds its semaphore while a task is in-flight — including while it's paused. With default `max_workers=5`, the 6th concurrent paused step blocks until one of the first five resumes.

**Design implication for T10:** for single-user laptop deployments, this is a non-issue (the user will generally have ≤1 pause active at a time). For the later "shared backend" milestone, `LocalExecutor.max_workers` needs to be sized against concurrent-approval volume, or we switch to a different execution model for steps that may pause for a long time (e.g., a dedicated "blocking-allowed" executor class).

Not urgent. Logged as a scope-item for the shared-backend phase.

### 3.3 Packaging: nanobrain's `pyproject.toml` is broken

`pip install -e /path/to/nanobrain` fails with a setuptools error:

> others must be specified via the equivalent attribute in `setup.py`

Also, `aiofiles` is used by nanobrain but not declared in its deps; importing nanobrain raises `ModuleNotFoundError: No module named 'aiofiles'` until you install it manually.

**Design implication for T10 (and for all integration work):**

- Real `apecx-mcp-integration` code cannot depend on `pip install nanobrain` working. We must either:
  - (a) bundle nanobrain as a vendored subtree in apecx-integration, or
  - (b) fix nanobrain's packaging (requires the "edit nanobrain discussed separately" approval), or
  - (c) do sys.path insertion (acceptable for spikes, NOT for shipped integration code).

**Recommendation:** Option (b). This is the cleanest path; it's one of the candidate edits for the batch-carve-out conversation.

---

## 4. What this spike does *not* prove

1. **It does not prove `ApprovalStep` works inside a full `Workflow`.** We tested `LocalExecutor.execute(coroutine)` in isolation. The next level is: does a `Workflow` that chains `Step A → ApprovalStep → Step B` actually pause correctly? That requires a minimal workflow wiring. Deferred to actual T10 implementation.
2. **It does not prove crash-recovery works.** The durability caveat (§3.1) is a *design* conclusion, not a tested behavior. T09 (durable state) + T10 integration testing is where that gets verified.
3. **It does not prove the pattern works with `ThreadExecutor` or `ProcessExecutor`.** Those serialize the callable across thread/process boundaries, which may have different semantics. For the local-default vertical slice (`LocalExecutor` only), this doesn't matter.

---

## 5. Implications for the implementation plan

| Task | Change |
|---|---|
| T10 | Stays at 6 code-days. Spike is GREEN — no scope explosion. |
| T10 design | Must specify HTTP-polling pattern (not in-process Event). Update T10 spec in `implementation_plan.md`. |
| Nanobrain packaging fix | NEW candidate scope-decision memo (let's call it `02_nanobrain_packaging_fix.md`) — small, discrete, clear ask for the batch carve-out. |
| T00.3 HPC spike | Unaffected. |
| T02r executor-decoupling | Unaffected. |

The spike unblocked T10 planning. Next concrete step is T09 (SQLAlchemy models + Alembic) because T10 needs the Control Plane to persist approvals.

---

## 6. Sign-off

_Verdict accepted? If yes, mark below and proceed with T10 per the caveats above._

- [ ] Verdict accepted; T10 can begin with the HTTP-polling pattern.
- [ ] Verdict accepted with scope adjustment (note below).
- [ ] Reject; specify concern.

Signature / date: ___________________________
