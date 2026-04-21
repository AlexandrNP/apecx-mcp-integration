# Scope Decision 02 — ApprovalStep in `nanobrain/` framework

**Date:** 2026-04-21
**Status:** **APPROVED** by user directive 2026-04-21.
**Triggered by:** T00.2 spike verdict (GREEN) + user choice to make `ApprovalStep` a framework primitive rather than an integration-layer class.
**Precedent:** Scope-decision 01 (Option C — nanobrain edits approved case-by-case).

---

## The decision

The `ApprovalStep` class — nanobrain's HITL-pause primitive — will live in `nanobrain/nanobrain/library/steps/approval_step.py` (or under a suitable subdirectory), not in `apecx-mcp-integration/`.

**Rationale:** `ApprovalStep` is a framework-level primitive. Any nanobrain user building an interactive workflow could reuse it. Putting it in `apecx-mcp-integration/` would make it private to this project and force other consumers to reimplement or copy-paste.

---

## Exact files that will change

### New in `nanobrain/`:

- `nanobrain/nanobrain/library/steps/__init__.py` — create if does not exist.
- `nanobrain/nanobrain/library/steps/approval_step.py` — the step class.
- `nanobrain/nanobrain/library/steps/approval_step.yml` — example config.
- `nanobrain/tests/unit/test_approval_step.py` — unit test with mock control-plane HTTP (paired with an integration test in `apecx-mcp-integration` that hits a real local Control Plane per the workspace mocks policy).

### Configuration surface (derived from T00.2 verdict):

The step does NOT use an in-process `asyncio.Event`. It POSTs to an external Control Plane URL and polls (or SSE-subscribes) for the decision. Config:

```yaml
name: synonym_approval_gate
description: Pause the workflow for human review of LLM-proposed synonyms.

input_data_units:
  proposals_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: proposals_input

output_data_units:
  approved_output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: approved_output

gate_policy:
  kind: hard                       # or soft with timeout
  timeout_seconds: null            # null = no timeout for hard; number for soft
  on_timeout: reject               # approved | rejected (for soft gates)

control_plane:
  base_url: "${CONTROL_PLANE_URL}"  # env-var interpolation
  poll_interval_seconds: 2.0
  request_timeout_seconds: 10.0
```

### Public interface (Python):

```python
class ApprovalStep(BaseStep):
    REQUIRED_CONFIG_FIELDS = ['name', 'gate_policy', 'control_plane']

    async def process(self, input_data: Dict[str, Any], **kwargs) -> Any:
        # 1. POST /approvals/ with the summary derived from input_data.
        # 2. Long-poll /approvals/{id}/decision until decided or timeout.
        # 3. Apply decision:
        #    - approved           -> return input_data unchanged
        #    - approved_with_mods -> apply modifications dict to input_data
        #    - rejected           -> raise StepRejected(reason)
        #    - timed_out (soft)   -> apply on_timeout policy
        ...
```

### Key behaviors (from T00.2 verdict §3.1)

- **No in-process Event.** All pause state lives in the Control Plane DB.
- **Restart recovery is driven by the Control Plane.** If the nanobrain process dies mid-pause, the Control Plane retains the pending approval. When a new `ApprovalStep.process()` is entered for the resumed run, it asks the Control Plane first; if a decision already exists, it applies immediately.
- **Semaphore caveat (T00.2 verdict §3.2).** For single-user laptop deployments this is a non-issue. Document the limit in the module docstring for future shared-deployment scope.

---

## Rollback plan

If the implementation reveals unforeseen coupling to nanobrain core:

1. Revert the three files above (`git revert` of the commit that adds them).
2. Move the class to `apecx-mcp-integration/src/apecx_integration/steps/approval_step.py` as a private subclass of `nanobrain.core.step.BaseStep`. It loses the "framework primitive" property but keeps the integration working.
3. Document the rollback as a scope-decision amendment.

---

## Dependencies

- T00.2 spike must be GREEN — done.
- T09 Control Plane durable state (Run, Step, Approval tables) must exist — not yet; T10 is gated on T09 progress.
- nanobrain packaging fix (scope-decision memo 03) — required for `apecx-mcp-integration` to `pip install nanobrain` cleanly. Currently we rely on sys.path insertion, which is acceptable for spikes but not for shipped integration code.

---

## Sign-off

- [x] **User approved 2026-04-21.** ApprovalStep goes in nanobrain.
- [ ] User confirms HARD gate is the default kind (open question 2 in workflow_spec.md).

Agent: Claude Code agent, 2026-04-21.
User directive:
> Option C. Approval step lives in nanobrain/ framework.
