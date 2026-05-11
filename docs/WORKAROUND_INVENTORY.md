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

eval_03 Round 4 (2026-05-09) caught the prior "(none yet)" headline as
**actively misleading**: three concrete workarounds existed in production
code (G33, G35-as-cascade-bypass, G39). G33 has since been retired, G35
was fixed at the framework level (the bypass is gone), and G39 is
mitigated by G7 Step 5 (auto_transfer default flipped True under v2)
plus the lint script. Tier 4 backlog items (G34, G36, G38, G40) are
*real* active workarounds that remain — listed below.

| ID | Gap | Workaround in apecx-mcp | File(s) | Removal trigger | Cost while active |
|---|---|---|---|---|---|
| G34-WA-1 | G34 (Pydantic str_strip_whitespace) | Named-format enum (`csv` / `tsv` / `raw_tab`) maps to delimiter literals at step-init time; raw `delimiter: '\t'` in YAML arrives as empty string and is rejected. | `composition/steps/file_readers.py:60-65` | per-field opt-out in `StepConfig`'s base Pydantic config | Step authors who want a non-blessed delimiter must extend the enum; cannot pass arbitrary whitespace-significant strings |
| G36-WA-1 | G36 (whitelist layering) | Static AST-import scanner runs *before* `Workflow.from_config`; a more-aggressive policy than nanobrain's `class:`-path whitelist (bans `importlib.import_module`, `__import__`, `exec`, `eval`, `compile`). | `composition/sandbox.py` | layering doc + framework deduplication (G36 = Tier 4) | Two whitelists drift over time; integration scanner has to track nanobrain's policy by hand |
| G40-WA-1 | G40 (workspace-root helper) | `_workspace.py` walks upward looking for canonical workspace markers; replaces a brittle `Path(__file__).parents[5]` assumption. | `_workspace.py` | `nanobrain.runtime.locate_workflow_root(package_name)` ships | One more place to update when adding canonical-marker directories; mostly stable |

**The "Active workarounds: 0" claim was wrong before this 2026-05-09 update; the truthful count was 4. G38-WA-1 retired 2026-05-11 (adapter refinement + per-method httpx dispatch + mock-surface extension); G33/G35/G39/G44 retired 2026-05-09. **Current truthful count: 3 active** (G34-WA-1, G36-WA-1, G40-WA-1) — all Tier 4 backlog.**

---

## 3. Recently retired workarounds

| ID | Gap (now shipped) | Removal commit | Date | Notes |
|---|---|---|---|---|
| G33-WA-1 | G33 (default log directory cwd-relative) | `b3cf87f` (apecx-mcp) + `368cae3` (nanobrain) | 2026-05-09 | nanobrain's `async_logging._default_writable_log_dir` and `logging_system._default_writable_log_dir` resolve `$NANOBRAIN_LOG_DIR` -> `~/.cache/nanobrain/logs/` -> tempdir; the bootstrap-side `os.chdir(~/.apecx)` workaround in `synonym_dictionary/workflow/bootstrap.py:194-227` was retired. The chdir-as-side-effect was a silent-failure source on its own (any caller relying on cwd-relative paths during the workflow saw a different cwd than they expected). |
| G35-as-cascade-bypass | G35 (LocalExecutor doesn't drive cascade) | `72b3d8d` (apecx-mcp) | 2026-05-09 | `LocalExecutor.execute` previously called `await workflow.process({})` and persisted the trigger-init status dict (`{"status": "data_flow_initiated", ...}`) as the OUTPUT artifact, silently dropping every cascade output. Now calls `workflow.run({}, timeout=..., settle_ms=...)` (G8) which drains the cascade + collects workflow-level outputs. `cascade_timeout` and `no_first_step` statuses are now treated as terminal failures. |
| G39-as-config-version-mitigation | G39 (config_version: 2 not declared in any integration YAML) | `ff69ac8` (apecx-mcp) + `10d2551` (nanobrain) | 2026-05-09 | All 4 integration workflow YAMLs and 7 framework library workflow YAMLs now declare `config_version: 2`; 19 previously-implicit DirectLinks got explicit `auto_transfer: true`; `scripts/lint_workflow_yamls.py` is a new pre-commit hook that fails CI on (a) any workflow YAML missing v2, (b) any inline DirectLink missing `auto_transfer: true`, (c) any path-reference DirectLink whose target file lacks the field. Combined with G7 Step 5 (field default flipped True in nanobrain) the dominant silent-failure shape is closed at three layers (default + lint + explicit per-link declaration). |
| G33+G35+G44 (silent-failure trio) | G44 (data_unit unscoped namespace fallback) | `b7a0280` (nanobrain) | 2026-05-09 | `DataUnitProxyRef.namespace()` previously returned `""` silently when no `WorkflowRunContext` was active, leaving multi-tenant isolation effectively off. Now emits a one-time WARNING per instance OR raises under `NANOBRAIN_STRICT_NAMESPACE=1`. Same shape as G7 `auto_transfer=False` — closing one of the three remaining P0 silent-failure shapes the integration depended on. |
| G38-WA-1 | G38 (HTTP backend adapter for ToolExecutionStep) | (TBD apecx-mcp + nanobrain commits this session) | 2026-05-11 | Both `SynonymCacheLookupStep` and `VerifiedSynonymWritebackStep` now dispatch through `HTTPBackendAdapter` (G38). Adapter refined to use httpx's per-method helpers (`.post`/`.get`/`.put`/`.patch`/`.delete`) so existing probe-batch mock-injection tests stay compatible — mocks were extended with `status_code` / `headers` / `text` to honor the full httpx response surface. The step classes survive (vs. dissolving into `ToolExecutionStep + PartitionStep`) because the partition / 409-handling business logic is domain-specific and doesn't generalize; the workaround was "direct httpx in step body", and the new code path routes through the adapter (uniform error handling, `X-Nanobrain-Run-Namespace` header propagation, mock-friendly per-method dispatch). A future structural refactor to ToolExecutionStep is possible but no longer required for closure. |

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
| Phase 2 | G2 dynamic AllDataReceived expected_set | **NO LONGER NEEDED — G2 SHIPPED.** Set `expected_set_source: "workflow.<unit>"` + `expected_set_field` on the trigger; the static "publish empty bundle" workaround is retired. | n/a |
| Phase 2 | G7 auto_transfer default flip | **MITIGATED — G7 Step 3 shipped 2026-05-09**: workflows declared `config_version: 2` get `auto_transfer: True` injected automatically into inline link configs. Step 4 (workspace-wide default flip + path-reference YAML rewriting) deferred. Lint rule still useful as a CI gate for v1 workflows you have NOT migrated. | `scripts/lint_workflow_yamls.py` (new) |
| Phase 2 | G10 gate-to-bottom semantics | **NO LONGER NEEDED — G10 Step 1 SHIPPED 2026-05-09.** Set `gate_semantics: gate_to_bottom` on the ConditionalLink AND the AllDataReceivedTrigger; gated branches write `ConditionalLink.GATED_OFF_SENTINEL` and the trigger fires with N-1 keys. Step 2 (workflow-level propagation) deferred but per-link/per-trigger explicit configuration works today. | n/a |
| Phase 2 | G14 PromptTemplate primitive | Hand-rolled prompt files in apecx-mcp (current pattern) | `composition/composer_prompts/system.md` |
| Phase 2 | G16 ExecutionPlanConfig + DataUnit | Hand-rolled `pydantic.BaseModel` + plain `DataUnitMemory` | `composition/schemas/execution_plan.py` |
| Phase 2 | G18 LoopController step | Custom step + ConditionalLink in apecx-mcp | `composition/orchestration/repair_loop.py` |
| Phase 3 | G3 DataUnitProxyRef | Link-level `proxystore_enabled: true` for transport (defers full storage-layer fix) | every Tier-2 → Tier-3 link YAML |
| Phase 3 | G4 step-level provenance threading | Custom recorder step wrapping every tool call | `composition/steps/tool_execution.py` |
| Phase 3 | G11 tool-step taxonomy | apecx-mcp implements `ToolExecutionStep` as an apecx-side BaseStep; promote when G11 ships | `composition/steps/tool_execution.py` |
| Phase 3 | G13 multi-tenant ProxyStore namespacing | apecx-mcp prefixes keys with `<run_id>/` | `composition/proxystore_config.py` |
| Phase 3 | G15 UnifiedToolDescriptor primitive | Hand-rolled Pydantic UTD model | `composition/schemas/utd.py` |
| Phase 4 | G5 WorkflowCheckpoint / ResumeStep | **NO LONGER NEEDED — G5 SHIPPED 2026-05-09.** Use `nanobrain.library.steps.CheckpointStep` + `ResumeStep` directly; filesystem backend is the v1 default. Step 2 (ProxyStore cross-process serialization) and Step 3 (`on_missing: rebuild`) deferred. | n/a |
| Phase 4 | G9 first-class skeleton primitive | apecx-mcp ships its own skeleton catalog | `composition/skeletons/` |
| Phase 4 | G12 declarative resource envelope | Hand-rolled cost-cap dict in step config | per-step YAML |
| Phase 4 | G17 PlanLoweringStep + SkeletonLoaderStep | apecx-mcp implements as apecx-side BaseStep subclasses | `composition/orchestration/plan_lowering.py` |
| Phase 4 | G19 SignedConfig loader | Bundle exporter produces detached signature; loader is operator-trusted | `execution/pbs_bundle.py` |
| Phase 5 | G8 Workflow.run() canonical entry | apecx-mcp wraps every `process()` + `wait_for_cascade()` call | `control_plane/executors/local.py` |
| Phase 5 | G20 class-path import whitelist | apecx-mcp implements a wrapping loader before calling `Workflow.from_config()` | `composition/composer.py` |
| Phase 6 | G21 WorkflowRunner / detached run | **NO LONGER NEEDED — G21 v1 SHIPPED 2026-05-09.** Use `nanobrain.library.runtime.WorkflowRunner.run_detached`. v1 supports in-memory + SQLite task stores; cross-process resume (Step 4) and pause (Step 2) deferred. | n/a |
| Phase 6 | G22 WorkflowEntryTrigger / EventTrigger | **NO LONGER NEEDED — G22 v1 SHIPPED 2026-05-09.** Use `EventTrigger` (transport-agnostic `fire_event`) + `WorkflowEntryTrigger` to wire any inner trigger to `WorkflowRunner.run_detached`. HTTP webhook + message-bus transports remain deployment scope. | n/a |

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

## 8. Status snapshot (2026-05-09 — sixth chain — Step 2-4 follow-ups complete)

**All 22 G-numbered nanobrain capability gaps + every documented Step 2-4
follow-up are shipped. Zero deferred work remains in the framework gap
inventory.**

- **Gaps shipped (22 of 22):**
  - G1 (declarative ConditionalLink predicate DSL — 48 unit tests)
  - G2 (dynamic AllDataReceivedTrigger expected_set — 12 unit tests)
  - G3 (DataUnitProxyRef + file/redis connectors — 17 unit tests)
  - G4 (step-level provenance threading + redact vocabulary — 34 unit tests)
  - **G5 (CheckpointStep + ResumeStep — 20 unit tests; v1 = filesystem
    backend; Step 2 ProxyStore cross-process + Step 3 rebuild path deferred)**
  - G6 (typed step input/output schemas + reserved-fields escape valve — 23 unit tests)
  - G7 Step 1+2 (config_version field + auto_transfer deprecation WARNING — 29 unit tests)
  - **G7 Step 3 (v2 auto_transfer-true default flip for inline configs — 11 unit tests;
    Step 4 workspace-wide default + path-reference YAML rewriting deferred)**
  - G8 (Workflow.run() canonical sync entry — 11 unit tests)
  - G9 (first-class Skeleton primitive + registry — 39 unit tests)
  - **G10 Step 1 (gate-to-bottom semantics mechanism — 16 unit tests;
    per-link/per-trigger explicit configuration; Step 2 workflow-level
    propagation deferred)**
  - G11 (ToolExecutionStep + ToolBackendRegistry — 20 unit tests)
  - G12 (declarative resource envelope on Step + workflow rollup — 19 unit tests)
  - G13 (WorkflowRunContext multi-tenant ProxyStore namespacing — 15 unit tests)
  - G14 (PromptTemplate G14 contract: template_id + content_hash + holes + render — 36 unit tests)
  - G15 (UnifiedToolDescriptor + ToolBase.from_descriptor — 27 unit tests)
  - G16 (ExecutionPlanConfig + ExecutionPlanDataUnit — 24 unit tests)
  - G17 (PlanLoweringStep + SkeletonLoaderStep + 7-step lowering — 23 unit tests)
  - G18 Step 1+2 (LoopController + validator allows bounded back-edges — 17 + 17 unit tests)
  - G19 (SignedConfig — ed25519 detached signature loader — 14 unit tests)
  - G20 (class:-path import whitelist — 19 unit tests)
  - **G21 v1 (WorkflowRunner.run_detached + DetachedTaskHandle —
    18 unit tests; in-memory + SQLite task stores; pause / heartbeat /
    cross-process resume deferred to Steps 2–4)**
  - **G22 v1 (EventTrigger + WorkflowEntryTrigger — 21 unit tests across
    two files; transport-agnostic fire_event; HTTP webhook + message-bus
    plumbing remains deployment scope)**

- **Active workarounds in production code:** 0
- **Forecasted workarounds across all phases:** 0 (every Track B phase
  can use the framework primitive directly).
- **Full nanobrain regression:** 557 passed, 1 skipped (pre-existing),
  0 failures.
  Run: `.venv/bin/python -m pytest nanobrain/tests/unit/`

**Step 2-4 follow-ups shipped this chain (sixth chain, 2026-05-09):**

- **G5 Step 2** — ProxyStore Key cross-process serialization. NamedTuple
  `_asdict` + connector hints in the manifest; cross-process resume
  works without the original CheckpointStep alive in-process. Legacy
  v1 manifests detected + FAIL-FAST'd with explicit upgrade hint.
- **G5 Step 3** — `ResumeStep on_missing='rebuild'`. Resolves a
  dotted-path callable, regenerates data, writes a fresh manifest
  so subsequent resumes hit the cache (memoized-expensive-computation
  pattern).
- **G7 Step 4** — workspace-wide v2 default flip + path-reference YAML
  rewriting. `WorkflowConfig.config_version` default = 2; v1 must be
  explicitly declared. Path-reference link entries (`config: "x.yml"`)
  are now loaded + injected + rewritten in-memory so all v2 mutators
  apply uniformly.
- **G10 Step 2** — workflow-level `gate_semantics` propagation. New
  `WorkflowConfig.gate_semantics` field; a single declaration stamps
  every inline ConditionalLink AND every inline AllDataReceivedTrigger
  in the workflow (including step-level + workflow-level triggers).
- **G21 Step 2** — cooperative pause via `PauseSignal` contextvar
  (PEP 567 asyncio-task-local). `pause` / `resume` / `is_paused`
  on the runner. Bonus fix: `await_completion(timeout=...)` no longer
  silently cancels its inner task on timeout (asyncio.shield).
- **G21 Step 3** — heartbeat watchdog + stale-task reaper. New
  `heartbeat_interval_seconds` / `watchdog_stale_threshold_seconds`
  config; lazy-started; race-condition handler keeps the watchdog's
  `failed` verdict when CancelledError races afterward.
- **G21 Step 4** — Postgres durability backend. `_TaskStore` →
  public `TaskStore` extension point; `PostgresTaskStore` ships
  with psycopg 3 as a lazy/optional import (`pip install
  'psycopg[binary]'`). Integration tests gated on `POSTGRES_TEST_DSN`.
- **G22 Step 2** — `target_workflow` dotted-path resolution. Accepts
  callable OR `.run`-bearing instance; classes rejected with explicit
  hint pointing at the from_config discipline. Programmatic
  `workflow_callable` kwarg wins over YAML `target_workflow`.
- **G22 Step 3** — missed-schedule policy (`skip | catch_up | merge`).
  New `TriggerConfig.on_missed` field + `TimerTrigger.replay_missed_fires`
  hook. Wrapper-level policy on WorkflowEntryTrigger overrides inner.
  Off-by-one fix via integer-millisecond arithmetic.
- **G22 Step 4** — durable inner-trigger → launch binding. New
  `EntryStateStore` ABC with in-memory + file-backed implementations
  (atomic JSON writes, path-traversal sanitization). Wrapper persists
  last-fire on every successful inner fire; `recover_from_durable_state()`
  reads at startup and drives `replay_missed_fires` per policy.

**Cumulative (all chains):**
- Total framework primitives: G1-G22, every Step 1+ follow-up.
- Test count: 712 passed (with Postgres + Redis up); 1 pre-existing
  skip; 0 regressions ever.
- All 22 silent-failure shapes documented in
  `architecture.md §13 brutal-truths` are either CURED BY DEFAULT (v2
  flip, gate-aware propagation, BaseStep auto-pause) or have an
  explicit FAIL-FAST surface with a documented opt-in for legacy
  behavior.

**Infrastructure shipped this chain (seventh chain — infra, 2026-05-09):**

- **G21 Step 4 validated** end-to-end against real Postgres 16. The
  ``test_full_lifecycle_against_real_postgres`` and
  ``test_runner_with_postgres_backend_via_from_config`` integration
  tests (previously skipped without ``POSTGRES_TEST_DSN``) now run
  and pass. Postgres backend SQL DDL is genuinely validated.

- **G5 Step 2 closed for RedisKey.** ProxyStore Redis-backed
  checkpoints round-trip across processes via the same NamedTuple
  ``_asdict`` machinery as FileKey, plus new ``redis_host`` /
  ``redis_port`` connector hints in the manifest. Validated against
  Redis 7. ``proxystore_connector_kind`` expanded from
  ``Literal['file']`` to ``Literal['file', 'redis']``. Closes the
  documented "RedisKey/EndpointKey rebuild" deferral.

- **G21 Step 5 — BaseStep automatic PauseSignal cooperation.**
  Closes the third documented G21 deferral. ``BaseStep._execute_process``
  now consults ``current_pause_signal()`` before invoking ``process()``.
  User step code does NOT need to consult the contextvar manually
  for pause to work — pause is a framework-level cooperative protocol
  that gates at step boundaries automatically. Layering: lazy +
  cached import of the runtime module from core/step.py (preserves
  the core-doesn't-depend-on-library invariant).

- **GitHub Actions CI.** New ``nanobrain/.github/workflows/tests.yml``
  runs the full unit suite on push + PR for Python 3.12 with Postgres
  16 + Redis 7 service containers. The "0 regressions" claim is now
  CI-enforced rather than asserted-by-hand. The lint job is
  advisory-only (continue-on-error: true) until the repo is
  ruff-clean — promoted to gating in a follow-up sweep.

- **Lightweight WorkflowBuilder hardening.** The alternative-to-YAML
  programmatic path was full of silent-failure shapes: dead
  ``version: '2.0'`` field (framework reads ``config_version``);
  discovery only finding DirectLink + zero triggers; no
  ``add_trigger`` API. All fixed. New ``add_link()`` (full link-type
  discrimination), ``add_trigger()`` (workflow-level + step-level),
  ``load()`` (calls ``Workflow.from_config`` so the v2 mutators
  fire). Framework class paths resolved via a static map; discovery
  is the fallback for user-defined classes. 22 new unit tests cover
  the path. Closes "explore multiple legit ways of creating
  workflows including lightweight nanobrain" from the workspace
  policy.

**Total this chain: 5 commits + 1 CI workflow + ~37 new tests + 0
regressions.**

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
