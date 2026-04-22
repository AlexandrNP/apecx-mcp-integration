# Scope Decision 05 — T02r executor-decoupling resolution

**Date:** 2026-04-22
**Status:** **Done** — nanobrain edit applied under the carve-out
established by scope memo 01 and the user's 2026-04-22 directive
("Proceed with TX3, T02r (check the current implementation, it should
already be step-specific and rather decoupled), and T12").

---

## Audit result

A read-only `Explore` subagent audited every step under
`nanobrain/nanobrain/library/workflows/viral_protein_analysis/steps/`
against the T02r goal ("executor is step-config-specified, not
hardcoded in Python"). Findings:

- **13 of 14 steps are already executor-agnostic.** No hardcoded
  executor imports, no direct `WorkQueueExecutor()` / `ParslExecutor()`
  instantiation, no implicit Aurora coupling in their `process()`
  methods.
- **1 step had a subtle residual coupling:**
  `nanobrain/library/workflows/viral_protein_analysis/steps/annotation_mapping_step.py`
  at line 344 called `self.executor.submit(self._execute_task, input_data)`.
  That pattern requires a distributed-executor API (`.submit()` returning
  a future). `LocalExecutor` does not implement `.submit()` — it only has
  the `execute()` coroutine. Running this step under `LocalExecutor`
  would `AttributeError` at the `.submit` call.
- **Workflow YAMLs** (`config/AlphavirusWorkflow.yml` and siblings) do
  not hardcode an executor; executor selection is already a workflow-
  runtime concern, not a YAML concern. No change needed.

---

## Fix applied

Minimum-surface patch in the one affected step. `process()` now branches
on runtime executor capability:

```python
if hasattr(self.executor, "submit"):
    # Distributed path (WorkQueue / Parsl / Globus-compute): unchanged.
    future = self.executor.submit(self._execute_task, input_data)
    result = await future.result()
else:
    # Local path (LocalExecutor): inline the same business logic.
    result = await self._execute_business_logic(input_data)
    # Preserve the ``worker_hostname`` field the distributed path
    # emits so downstream consumers stay agnostic.
```

Both paths now run to completion regardless of the injected executor.
No import added; no dependency added; `_execute_business_logic` was
already a sibling method of `_execute_task`.

---

## What this is NOT

- **Not a YAML-level per-step executor selector.** The T02r plan text
  entertained that as the "goal-aligned" shape, but it was predicated
  on multiple steps being hardcoded. With 13/14 already agnostic the
  YAML selector would be speculative infrastructure. Defer until a
  second step needs it.
- **Not an end-to-end integration test.** The T02r fix is verified via
  syntax parse and the audit's static analysis of the only affected
  line. The actual behavior (LocalExecutor running `annotation_mapping_step`
  end-to-end) lands when T01 (vertical-slice integration test) exercises
  this step on a laptop run of the `viral_protein_analysis` workflow.
  Adding a standalone nanobrain unit test here would require mocking
  the workflow's upstream state, which conflicts with the workspace
  mock-policy on nanobrain.

---

## Files changed

- `nanobrain/nanobrain/library/workflows/viral_protein_analysis/steps/annotation_mapping_step.py`
  — `process()` method, runtime executor-capability branch.

`nanobrain/` is not a git repo; changes live on disk under user-approved
carve-out scope.

---

## Related scope memos

- **01** — where new code lives (case-by-case nanobrain edits).
- **03** — nanobrain packaging fixes (aiofiles); the aiohttp /
  aiosqlite gaps noted during this audit are of the same shape and
  queue for the same remediation.
