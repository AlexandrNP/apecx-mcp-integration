# Workaround Inventory

**Status:** Live tracker — drive to zero as gaps ship.
**Audience:** Implementers, reviewers, project lead
**Owner:** Whoever last shipped a gap fix or added a workaround

---

## 1. Why this file exists

Per `implementation_task_graph.md` MC-X-01: every framework gap (G1–G22) that
hasn't shipped yet is paired with an apecx-mcp-side **workaround** that lets
Track B move in parallel with Track A. Each workaround is a documented compromise:
it works, it's tested, but it carries cost (boilerplate, performance, or
provenance fidelity) that goes away when the corresponding gap ships.

This file lists every workaround currently in apecx-mcp-integration. When a gap
ships, the corresponding workaround removal becomes the next task.

**The table below is the source of truth. If a workaround exists in code but is
not listed here, that is a process bug — add the row in the same PR that adds
the workaround.**

---

## 2. Active workarounds

| ID | Gap | Workaround in apecx-mcp | File(s) | Removal trigger | Cost while active |
|---|---|---|---|---|---|
| (none yet — apecx-mcp does not yet have any G1–G22-paired workarounds in production code; the new design package is pre-implementation) | | | | | |

---

## 3. Recently retired workarounds

| ID | Gap (now shipped) | Removal commit | Date | Notes |
|---|---|---|---|---|
| (none yet — the package has no historical workarounds to retire) | | | | |

---

## 4. Workarounds we will need as we ship Track B

Per `development_roadmap.md` per-phase gap dependency tables, the following
workarounds will need to be added when their respective Track B phases ship,
unless the corresponding Track A gap ships first.

This forecast is informational — actual workarounds get added in the same PR
that ships the consuming code.

| Phase | Gap | Forecasted workaround | Likely file location |
|---|---|---|---|
| Phase 1 | G6 typed result schemas | Hand-rolled Pydantic `LayerResult` in apecx-mcp | `composition/schemas/layer_result.py` |
| Phase 2 | G1 declarative ConditionalLink predicate DSL | **NO LONGER NEEDED — G1 SHIPPED 2026-05-09.** Use the new `op:`-keyed predicate form directly in YAML. | n/a |
| Phase 2 | G2 dynamic AllDataReceived expected_set | "Publish empty bundle" sentinel from every gated layer step | `composition/steps/layers/*.py` |
| Phase 2 | G7 auto_transfer default flip | Lint rule rejecting any DirectLink YAML without explicit `auto_transfer: true`. **PARTIALLY MITIGATED — G7 Step 1+2 shipped 2026-05-09**: nanobrain now WARNS at workflow load when v1 omits `auto_transfer`. Lint rule still useful as a CI gate (catches at PR time, not at runtime). | `scripts/lint_workflow_yamls.py` (new) |
| Phase 2 | G10 gate-to-bottom semantics | Phase0PlanningStep refuses to emit empty `active_layers`; every workflow has at least one always-true layer | `composition/steps/phase0_planning.py` |
| Phase 2 | G14 PromptTemplate primitive | Hand-rolled prompt files in apecx-mcp (current pattern) | `composition/composer_prompts/system.md` |
| Phase 2 | G16 ExecutionPlanConfig + DataUnit | Hand-rolled `pydantic.BaseModel` + plain `DataUnitMemory` | `composition/schemas/execution_plan.py` |
| Phase 2 | G18 LoopController step | Custom step + ConditionalLink in apecx-mcp | `composition/orchestration/repair_loop.py` |
| Phase 3 | G3 DataUnitProxyRef | Link-level `proxystore_enabled: true` for transport (defers full storage-layer fix) | every Tier-2 → Tier-3 link YAML |
| Phase 3 | G4 step-level provenance threading | Custom recorder step wrapping every tool call | `composition/steps/tool_execution.py` |
| Phase 3 | G11 tool-step taxonomy | apecx-mcp implements `ToolExecutionStep` as an apecx-side BaseStep; promote when G11 ships | `composition/steps/tool_execution.py` |
| Phase 3 | G13 multi-tenant ProxyStore namespacing | apecx-mcp prefixes keys with `<run_id>/` | `composition/proxystore_config.py` |
| Phase 3 | G15 UnifiedToolDescriptor primitive | Hand-rolled Pydantic UTD model | `composition/schemas/utd.py` |
| Phase 4 | G5 WorkflowCheckpoint / ResumeStep | apecx-mcp persists tournament state in control plane DB | `control_plane/models/tournament_state.py` |
| Phase 4 | G9 first-class skeleton primitive | apecx-mcp ships its own skeleton catalog | `composition/skeletons/` |
| Phase 4 | G12 declarative resource envelope | Hand-rolled cost-cap dict in step config | per-step YAML |
| Phase 4 | G17 PlanLoweringStep + SkeletonLoaderStep | apecx-mcp implements as apecx-side BaseStep subclasses | `composition/orchestration/plan_lowering.py` |
| Phase 4 | G19 SignedConfig loader | Bundle exporter produces detached signature; loader is operator-trusted | `execution/pbs_bundle.py` |
| Phase 5 | G8 Workflow.run() canonical entry | apecx-mcp wraps every `process()` + `wait_for_cascade()` call | `control_plane/executors/local.py` |
| Phase 5 | G20 class-path import whitelist | apecx-mcp implements a wrapping loader before calling `Workflow.from_config()` | `composition/composer.py` |
| Phase 6 | G21 WorkflowRunner / detached run | apecx-mcp implements a custom runner without G5 checkpoint integration; tasks fail closed on restart | `control_plane/runtime/autonomous_service.py` |
| Phase 6 | G22 WorkflowEntryTrigger / EventTrigger | apecx-mcp implements a separate scheduler service that calls `start_workflow` MCP synchronously | `control_plane/runtime/scheduler.py` |

---

## 5. Process — adding a workaround

When a Track B task needs a primitive that's not yet shipped:

1. **Confirm the gap is in `nanobrain_capability_gaps.md`.** If the missing
   primitive is genuinely new, add a gap entry first (G23+).
2. **Add a row to §2 (Active workarounds)** in the same PR that adds the
   workaround code. Use the gap ID (e.g., `G3-WA-1`) for the workaround ID.
3. **Code-comment the workaround** with `# G<N>-WORKAROUND: ...` so a grep
   finds it. Include the inventory ID and the removal trigger.
4. **Reference the inventory ID in the PR description** so a reviewer can
   check that the row was added.

## 6. Process — removing a workaround

When a Track A gap ships:

1. **Mark the gap entry** in `nanobrain_capability_gaps.md` with
   "Status: SHIPPED YYYY-MM-DD" so it's discoverable.
2. **Move the workaround row** from §2 to §3 (Recently retired) with the
   removal commit hash.
3. **Delete the workaround code** in apecx-mcp; replace with the framework
   primitive call. The PR title should match the pattern
   `Retire G<N>-WA-<id> — replace workaround with framework primitive`.
4. **Update tests** — the workaround's tests should be replaced with tests
   that exercise the framework primitive end-to-end.

---

## 7. Cross-references

- `implementation_task_graph.md` — full task graph; MC-X-01 is the task that
  created this file
- `nanobrain_capability_gaps.md` — G1-G22 gap proposals; each is paired with
  a workaround in this file until shipped
- `development_roadmap.md` — per-phase "Framework gap dependencies" tables
  list the workarounds each phase implements
- workspace `CLAUDE.md` — the silent-failure carve-out rule that motivates
  the lint discipline around `auto_transfer: true`

---

## 8. Status snapshot (2026-05-09 — extended)

- **Gaps shipped:**
  - G1 (declarative ConditionalLink predicate DSL — 48 unit tests)
  - G2 (dynamic AllDataReceivedTrigger expected_set — 12 unit tests)
  - G3 (DataUnitProxyRef + file/redis connectors — 17 unit tests)
  - G6 (typed step input/output schemas + reserved-fields escape valve — 23 unit tests)
  - G7 Step 1+2 (config_version field + auto_transfer deprecation WARNING — 29 unit tests)
  - G8 (Workflow.run() canonical sync entry — 11 unit tests)
  - G13 (WorkflowRunContext multi-tenant ProxyStore namespacing — 15 unit tests)
  - G14 (PromptTemplate G14 contract: template_id + content_hash + holes + render — 36 unit tests)
  - G15 (UnifiedToolDescriptor + ToolBase.from_descriptor — 27 unit tests)
  - G16 (ExecutionPlanConfig + ExecutionPlanDataUnit — 24 unit tests)
  - G18 Step 1+2 (LoopController + validator allows bounded back-edges — 17 + 17 unit tests)
- **Active workarounds in production code:** 0
- **Forecasted workarounds across all phases:** 9 (down from 21 — 12
  fewer because G1/G2/G3/G6/G7/G8/G13/G14/G15/G16/G18 shipped)
- **Full nanobrain regression:** 303 passed, 1 skipped (pre-existing),
  0 failures. Run command:
  `.venv/bin/python -m pytest nanobrain/tests/unit/`

**Gaps still pending:**
- G4 (step-level provenance threading) — P1
- G5 (WorkflowCheckpoint / ResumeStep) — P1
- G7 Steps 3+4 (auto_transfer default flip) — needs cross-repo bake time
- G9 (first-class skeleton primitive) — P0
- G10 (gate-to-bottom semantics for ConditionalLink + AllDataReceived) — deferred (deep change to _monitor_all_data; risks G2 regression without dedicated session)
- G11 (tool-step taxonomy) — P1
- G12 (resource envelope on Step) — P1
- G17 (PlanLoweringStep + SkeletonLoaderStep) — P0 (depends on G9)
- G19 (SignedConfig loader) — P2
- G20 (class: import whitelist) — P2
- G21 (Workflow.run_detached) — P1
- G22 (WorkflowEntryTrigger / EventTrigger) — P1

**Track C (Rhea fork):**
- T-RH-00 (fork creation) + T-RH-01 (extension scaffold) shipped to
  `AlexandrNP/rhea` `apecx-integration` branch (commit `4ff9493`).
- T-RH-02..T-RH-09 deferred — need Rhea runtime infrastructure
  (Postgres + Redis + MinIO + Parsl) to integration-test honestly.
  G15 UTD primitive is now shipped, so T-RH-02 (UTD producer) is
  unblocked from a framework-side standpoint.

The "0 active workarounds" status is a function of pre-implementation
design — not a sign that the Track B phases will ship without workarounds.
Update this section every PR that adds or retires a workaround.
