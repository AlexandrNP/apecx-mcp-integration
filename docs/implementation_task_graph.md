# APECx Implementation Task Graph

**Status:** Implementation plan / pre-execution
**Audience:** Implementers (nanobrain framework, apecx-mcp-integration, Rhea fork), reviewers, project leads
**Supplements:** `_design_index.md` (the 17-doc design package), `nanobrain_capability_gaps.md` (G1–G20), `development_roadmap.md` (phase plan)
**Authoritative for:** Concrete file-level work, ownership, dependencies, definition of done

---

## 1. Purpose and reading guide

The design package (`_design_index.md`) tells us **what** to build. The development
roadmap tells us **in what phases**. This document tells us **which files in
which repos**, **in what order**, and **when each task is "done".**

Each task has a stable ID, a one-paragraph description grounded in the actual
code paths, dependencies on other task IDs, an effort estimate, and a
definition of done that an implementer can check against before claiming
the task complete.

The graph is split into four tracks. Tasks within a track are sequenced by
their dependencies; tasks across tracks can run in parallel where
dependencies allow.

| Track | Repo | Owner role | What it builds |
|---|---|---|---|
| **A — nanobrain framework** | `nanobrain/` | Framework contributor | The 20 capability-gap fixes (G1–G20) + version-bump support |
| **B — apecx-mcp-integration** | `apecx-mcp-integration/` | Application engineer | Orchestrator, skeleton library, Tier-1/2/3 agents, control-plane extensions |
| **C — Rhea fork** | (new fork to create) | Tool-platform engineer | UTD support, multi-tenant ProxyStore namespace, provenance cooperation |
| **D — Cross-track integration** | spans all three | Project lead + on-call | End-to-end tests, HPC bundle round-trip, deployment artifacts |

Track A unblocks Track B and Track C by shipping primitives. Track B can ship
**workarounds** for any unshipped Track-A primitive (per the per-phase
gap-dependency tables in `development_roadmap.md`), so Track B is not strictly
serialized behind Track A — it can move in parallel by paying the workaround
cost.

**Effort buckets:** S (≤1 day), M (1-3 days), L (3-7 days), XL (>1 week).
These are calendar-time approximations for one implementer; actual variance
is high.

---

## 2. Track A — Nanobrain framework gap fixes

Each gap in `nanobrain_capability_gaps.md` becomes 2-5 implementation tasks.
Tasks are prefixed `NB-G<N>-<NN>`.

### G1 — Declarative ConditionalLink predicate DSL

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G1-01 | Define `PredicateConfig(ConfigBase)` Pydantic model with fixed `op` vocabulary | `nanobrain/core/link.py` (new class near `LinkConfig` at l.200) | — | S | Class loads via `ConfigBase.from_config`; rejects unknown ops; rejects mixed leaf+combinator fields |
| NB-G1-02 | Implement `_evaluate_predicate(payload, predicate)` dotted-path resolver | `nanobrain/core/link.py` (new private fn) | NB-G1-01 | S | Unit test: leaf ops over dict + Pydantic model payloads; raises on miss when `op != "exists"` |
| NB-G1-03 | Wire `PredicateConfig` into `ConditionalLink.from_config` (l.1653) | `nanobrain/core/link.py` (extend ConditionalLink) | NB-G1-01, NB-G1-02 | S | Existing callable-predicate path still works; new declarative-predicate path returns same bool result for equivalent semantics |
| NB-G1-04 | Workflow integrity validator: reject mixed v1+v2 predicates in one workflow | `nanobrain/core/workflow.py` (extend integrity validator) | NB-G1-03 | S | Workflow with one callable-predicate + one declarative-predicate ConditionalLink raises `FAIL-FAST: ...` |
| NB-G1-05 | Tests + skill-doc update | `nanobrain/tests/core/test_link_predicates.py` (new); `.claude/skills/nanobrain-data-units-triggers-links/SKILL.md` (extend) | NB-G1-04 | M | All ops covered; YAML round-trip example added to skill |

**Total for G1:** ~1 week. Unblocks Track B's Phase 2 layered-reasoning workflow.

### G2 — Dynamic AllDataReceived expected_set

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G2-01 | Add `expected_set_source`, `expected_set_field`, `expected_set_naming` fields to `TriggerConfig` | `nanobrain/core/trigger.py` (extend `TriggerConfig` at l.243) | — | S | Fields default to None; existing static-list YAMLs load unchanged |
| NB-G2-02 | Implement `_resolve_expected_set()` on `AllDataReceivedTrigger` (l.1181) | `nanobrain/core/trigger.py` | NB-G2-01, NB-G1-02 (reuse dotted-path resolver) | M | One-shot resolution on first activation; FAIL-FASTs on missing source or off-DAG name |
| NB-G2-03 | Workflow-level data unit access protocol — trigger reads `workflow.<unit_name>` | `nanobrain/core/workflow.py` + `nanobrain/core/trigger.py` | NB-G2-02 | M | Trigger can read a workflow-level data unit at activation time; verified with integration test |
| NB-G2-04 | Tests | `nanobrain/tests/core/test_trigger_dynamic_set.py` (new) | NB-G2-03 | M | Static-list path unchanged; dynamic path resolves correctly under multiple projection shapes |

**Total for G2:** ~1 week. Eliminates the "publish empty bundle" workaround in Track B.

### G3 — DataUnitProxyRef

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G3-01 | Add `proxystore` extra to `nanobrain/pyproject.toml`; verify `proxystore-py` installs | `nanobrain/pyproject.toml` | — | S | `pip install -e '.[proxystore]'` succeeds in venv |
| NB-G3-02 | Define `DataUnitProxyRef(DataUnitBase)` with file/redis/globus connectors | `nanobrain/core/data_unit.py` (new class after `DataUnitStream` at l.1742) | NB-G3-01 | L | Set/get round-trip works against file connector; Redis path tested when Redis available |
| NB-G3-03 | Implement `__eq__` / `__hash__` based on `(namespace, key)` per spec | `nanobrain/core/data_unit.py` | NB-G3-02 | S | Two refs with same (ns, key) are equal; metadata differences ignored |
| NB-G3-04 | Implement `as_proxy()` for fan-out without materialization | `nanobrain/core/data_unit.py` | NB-G3-02 | S | Returns a Proxy<T> object; `.get()` on proxy materializes lazily |
| NB-G3-05 | Update `AllDataReceivedTrigger` change-event semantics: fire on key-set, not on materialization | `nanobrain/core/trigger.py` (extend) | NB-G3-02 | S | Trigger fires when a `DataUnitProxyRef.set()` completes (key written), not on `.get()` |
| NB-G3-06 | Tests + integration test | `nanobrain/tests/core/test_proxy_ref.py`, `nanobrain/tests/integration/test_proxy_ref_redis.py` | NB-G3-05 | M | File connector path: unit; Redis connector path: integration with skipif on Redis availability |

**Total for G3:** ~1.5 weeks. Unblocks Track B Phase 3 (HPC-scale tool I/O) and Track C Rhea fork ProxyStore handoff.

### G4 — Step-level provenance threading + redact vocabulary

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G4-01 | Define `ProvenanceContext(FromConfigBase)` + `JsonlSink` | `nanobrain/core/provenance.py` (new file) | — | M | from_config loads sink; `record_step_invocation` writes JSONL line |
| NB-G4-02 | Wrap `BaseStep._execute_process` to call `ProvenanceContext.record_step_invocation` | `nanobrain/core/step.py` (extend) | NB-G4-01 | M | Every `process()` call produces a record, including failures (with `exception` field) |
| NB-G4-03 | Implement `redact:` filter primitives (`payload`, `tool_args`, `prompts`, `llm_completions`, `executor_env`, `path:<dotted>`) | `nanobrain/core/provenance.py` (extend) | NB-G4-01 | M | Each primitive replaces the named field with the typed marker per spec; `path:<dotted>` resolves via shared dotted-path resolver |
| NB-G4-04 | Default redaction (`["prompts", "executor_env"]`) when `redact:` omitted | `nanobrain/core/provenance.py` | NB-G4-03 | S | Workflow without `redact:` config produces records with prompts and env-vars elided |
| NB-G4-05 | `ProxyStoreRef`-aware payload hashing (records key+size, not bytes) | `nanobrain/core/provenance.py` | NB-G3-02 | S | Record for a step that wrote a `DataUnitProxyRef` carries `{"key": "...", "size_bytes": N, "hash": "..."}` |
| NB-G4-06 | Tests | `nanobrain/tests/core/test_provenance.py`, `nanobrain/tests/integration/test_provenance_redaction.py` | NB-G4-05 | M | All redact primitives covered; default + explicit redact lists tested; failure-path records present |

**Total for G4:** ~2 weeks.

### G5 — WorkflowCheckpoint / ResumeStep

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G5-01 | Define `CheckpointStep(BaseStep)` | `nanobrain/library/steps/checkpoint_step.py` (new) | NB-G3-02, NB-G13-01 | M | `process()` snapshots configured data units; writes `checkpoint_manifest.json`; idempotent re-runs |
| NB-G5-02 | Define `ResumeStep(BaseStep)` | `nanobrain/library/steps/resume_step.py` (new) | NB-G5-01 | M | `process()` loads manifest, restores data units, signals upstream skip |
| NB-G5-03 | Workflow runner: honor `ResumeStep` upstream-skip signal | `nanobrain/core/workflow.py` (extend orchestrator) | NB-G5-02 | L | Resumed workflow does not re-run upstream steps whose data units were restored |
| NB-G5-04 | Code-identity recording + container-digest mismatch policy | `nanobrain/library/steps/resume_step.py` | NB-G5-02 | S | git sha mismatch = WARNING; container digest mismatch = FAIL-FAST unless `--accept-container-mismatch` |
| NB-G5-05 | Validator: reject `DataUnitStream` in `capture:` list | `nanobrain/core/workflow.py` (extend integrity validator) | NB-G5-01 | S | Workflow load FAIL-FASTs with clear message |
| NB-G5-06 | Tests + integration test | `nanobrain/tests/library/test_checkpoint_resume.py` | NB-G5-05 | M | Full snapshot+resume round-trip; partial-failure interaction; conditional-gating preservation |

**Total for G5:** ~2 weeks.

### G6 — Typed result schemas at framework boundary

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G6-01 | Define `SchemaRef(ConfigBase)` (Pydantic class path OR JSON Schema) | `nanobrain/core/step.py` (extend `StepConfig`) | — | S | Both shapes load; reject if both fields set |
| NB-G6-02 | Add `step_input_schema` and `step_output_schema` to `StepConfig` | `nanobrain/core/step.py` | NB-G6-01 | S | Fields default None (no validation); existing configs unchanged |
| NB-G6-03 | Validate inputs and outputs in `_execute_process` wrapper | `nanobrain/core/step.py` | NB-G6-02, NB-G4-02 (interaction order) | M | FAIL-FAST on schema mismatch; reserved fields (`errors`, `partial`) admitted per escape valve |
| NB-G6-04 | Reserved-fields escape-valve enforcement: validate `errors` against `StepError` shape, `partial` as bool | `nanobrain/core/step.py` | NB-G6-03 | S | Step may write `errors` and `partial` even when not declared in schema; reserved-name shape is enforced |
| NB-G6-05 | `validate_on_set: true` for ProxyRef payloads (forces materialization at write time) | `nanobrain/core/data_unit.py` | NB-G3-02, NB-G6-03 | S | Producer that opts in materializes proxy before validation; default behavior is validate-on-get |
| NB-G6-06 | Workflow-level `require_schemas: true` strict mode | `nanobrain/core/workflow.py` | NB-G6-03 | S | When set, every Step in the workflow must declare both schemas; load FAIL-FASTs otherwise |
| NB-G6-07 | Tests | `nanobrain/tests/core/test_step_schemas.py` | NB-G6-06 | M | Pydantic + JSON Schema paths; escape-valve usage; ProxyRef interaction; strict-mode rejection |

**Total for G6:** ~1.5 weeks. Unblocks Track B's typed `LayerResult` (Phase 1).

### G7 — DirectLink auto_transfer default flip

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G7-01 | Add `config_version: Literal[1, 2] = 1` to `WorkflowConfig` | `nanobrain/core/workflow.py` | — | S | YAMLs without `config_version` load as v1; explicit `config_version: 2` accepted |
| NB-G7-02 | Resolve `auto_transfer: None` against `config_version` in workflow loader | `nanobrain/core/workflow.py` (loader) + `nanobrain/core/link.py` (DirectLinkConfig) | NB-G7-01 | M | v1 → False; v2 → True; explicit values respected; deprecation WARNING when v1 omits the field |
| NB-G7-03 | Audit + migrate `nanobrain/library/workflows/*.yml` (~12 files) | `nanobrain/library/workflows/` (every .yml) | NB-G7-02 | M | Each workflow either pinned to v1 with comment or migrated to v2 with explicit `auto_transfer:` flags; library tests still green |
| NB-G7-04 | Apply same flip to `TransformLink` and `ConditionalLink` | `nanobrain/core/link.py` (extend defaults) | NB-G7-02 | S | All `LinkBase` subclasses inherit the v2 default-True behavior |
| NB-G7-05 | Tests | `nanobrain/tests/core/test_config_version.py` | NB-G7-04 | M | v1+v2 mixed-load test; deprecation WARNING captured; no regression in library workflows |

**Total for G7:** ~1.5 weeks. Eliminates the dominant silent-failure shape across the codebase.

### G8 — Workflow.run() canonical synchronous entry

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G8-01 | Implement `Workflow.run(input, await_cascade=True, timeout, settle_ms)` | `nanobrain/core/workflow.py` | — | M | Internally calls `process()` then `wait_for_cascade()`; collects workflow-level outputs into return dict |
| NB-G8-02 | Tests | `nanobrain/tests/core/test_workflow_run.py` | NB-G8-01 | S | `run()` returns terminal-state outputs; `process()` preserved unchanged |
| NB-G8-03 | Update apecx-mcp call sites that wrap `process()` + `wait_for_cascade()` to call `run()` | `apecx-mcp-integration/src/apecx_integration/control_plane/executors/local.py` (Track B) | NB-G8-01 | S | Call sites simplify; behavior unchanged |

**Total for G8:** ~3 days.

### G9 — First-class skeleton primitive

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G9-01 | Define `Skeleton(ConfigBase)` with hole grammar (per `agent_workflow_authoring.md §4.1`) | `nanobrain/library/orchestration/skeleton.py` (new) | — | M | `Skeleton.from_yaml(path)` + `Skeleton.from_yaml_with_holes_filled(bindings)`; rejects unfilled holes when load_filled=True |
| NB-G9-02 | Skeleton registry — content-addressed lookup | `nanobrain/library/orchestration/skeleton_registry.py` (new) | NB-G9-01 | M | Lookup by SHA-256 digest or semver tag; tag→digest mapping cached |
| NB-G9-03 | Skeleton schema validator — every hole declared in schema, every reference in YAML matches | `nanobrain/library/orchestration/skeleton.py` | NB-G9-01 | S | Skeleton with undeclared hole or undefined reference FAIL-FASTs at load |
| NB-G9-04 | Tests | `nanobrain/tests/library/test_skeleton.py` | NB-G9-03 | M | Hole substitution, version pinning, registry lookup, schema validation |

**Total for G9:** ~1.5 weeks. Unblocks Track B Strategy A authoring (Phase 4).

### G10 — ConditionalLink + AllDataReceived deadlock fix

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G10-01 | Implement gate-to-bottom semantics: a ConditionalLink that gates off propagates a sentinel that AllDataReceivedTrigger interprets as "this input is permanently absent" | `nanobrain/core/link.py` + `nanobrain/core/trigger.py` | NB-G2-02 | M | Workflow with all layers gated off does not deadlock; trigger fires with empty active set |
| NB-G10-02 | Validator: reject workflow where all upstream inputs to an AllDataReceivedTrigger are statically gated off (would always deadlock) | `nanobrain/core/workflow.py` (extend integrity validator) | NB-G10-01 | S | Static analysis catches the trivial deadlock case |
| NB-G10-03 | Tests | `nanobrain/tests/integration/test_conditional_alldatareceived.py` | NB-G10-02 | M | All-gated-off workflow behaves correctly; partial-gated workflow waits only for active inputs |

**Total for G10:** ~1 week.

### G11 — Tool-step taxonomy

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G11-01 | Define `ToolExecutionStep(BaseStep)` base class | `nanobrain/library/steps/tool_execution_step.py` (new) | NB-G15-01 (UTD) | M | Consumes a UTD reference; dispatches to backend adapter; emits typed output via `step_output_schema` |
| NB-G11-02 | Backend adapter protocol — `RheaAdapter`, `LocalParslAdapter` | `nanobrain/library/steps/tool_adapters/` (new dir) | NB-G11-01 | L | Each adapter is a `ToolBase` subclass that knows how to invoke its backend; both share the UTD-driven interface |
| NB-G11-03 | Cost + capability declaration — read from UTD `cost_estimate` and `requires_capability` | `nanobrain/library/steps/tool_execution_step.py` | NB-G11-01 | S | Step exposes `resource_envelope` derived from UTD; consumed by Phase 5 (G12) |
| NB-G11-04 | Tests | `nanobrain/tests/library/test_tool_execution_step.py` | NB-G11-03 | M | Mock adapter for unit; real Rhea adapter integration test (gated on Track C T-RH-04) |

**Total for G11:** ~2 weeks.

### G12 — Declarative resource envelope on Step

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G12-01 | Define `ResourceEnvelope(ConfigBase)` | `nanobrain/core/step.py` (new sibling to `StepConfig`) | — | S | Walltime, cpu, memory, capability_tokens, cost_units fields; extra: forbid |
| NB-G12-02 | Add `resource_envelope: Optional[ResourceEnvelope]` to `StepConfig` | `nanobrain/core/step.py` | NB-G12-01 | S | Existing configs unchanged; envelope is optional |
| NB-G12-03 | Implement `Workflow.aggregate_resource_envelope()` | `nanobrain/core/workflow.py` | NB-G12-02 | M | Aggregates per declared per-field rule (sum/max/union); used by HPC bundle exporter |
| NB-G12-04 | Tests | `nanobrain/tests/core/test_resource_envelope.py` | NB-G12-03 | M | Per-step envelope round-trip; aggregation rules verified |

**Total for G12:** ~1 week.

### G13 — Multi-tenant ProxyStore namespacing

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G13-01 | Define `WorkflowRunContext(FromConfigBase)` carrying `run_id`, `start_time`, `proxystore_namespace` | `nanobrain/core/workflow.py` | — | M | Created at `Workflow.run()` entry; accessible to all steps in the run |
| NB-G13-02 | `DataUnitProxyRef._namespaced_key()` prefixes with `run_<run_id>/` | `nanobrain/core/data_unit.py` | NB-G3-02, NB-G13-01 | S | Two concurrent runs do not collide; unscoped namespace WARNING when no run context |
| NB-G13-03 | `run_id` propagates into `ProvenanceContext` records (G4) and `CheckpointStep` manifests (G5) | `nanobrain/core/provenance.py`, `nanobrain/library/steps/checkpoint_step.py` | NB-G13-01 | S | Provenance and checkpoint records carry `run_id` |
| NB-G13-04 | Tests | `nanobrain/tests/core/test_run_context.py`, `nanobrain/tests/integration/test_multi_run_proxystore.py` | NB-G13-03 | M | Concurrent-run isolation verified |

**Total for G13:** ~1 week.

### G14 — PromptTemplate primitive

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G14-01 | Audit existing `nanobrain/core/prompt_template_manager.py`; identify what's already there vs. what G14 adds | `nanobrain/core/prompt_template_manager.py` (read) | — | S | Audit doc 1-page summary committed; gap between current and target enumerated |
| NB-G14-02 | Define `PromptTemplate(ConfigBase)` per `llm_prompt_contracts.md §3` schema | `nanobrain/core/prompt_template.py` (new or extend manager) | NB-G14-01 | M | Holes, model_constraint, system_prompt, user_template, output_schema, gates fields; extra: forbid |
| NB-G14-03 | Define `PromptTemplateManager` loader with content-addressing | `nanobrain/core/prompt_template.py` | NB-G14-02 | M | Loads by `template_id` (semver); pins by content_hash; caches by hash |
| NB-G14-04 | Hole substitution + few-shot bundling | `nanobrain/core/prompt_template.py` | NB-G14-02 | M | Substitution validates required holes; few-shot examples are appended per template policy |
| NB-G14-05 | Provenance integration: log `template_id`, `content_hash`, `param_hash` per LLM call (G4) | `nanobrain/core/prompt_template.py` + `nanobrain/core/agent.py` | NB-G14-04, NB-G4-02 | S | Every Agent LLM call produces a provenance record carrying the template fingerprint |
| NB-G14-06 | Tests | `nanobrain/tests/core/test_prompt_template.py` | NB-G14-05 | M | Round-trip load + render; missing-hole error; few-shot bundling; provenance recording |

**Total for G14:** ~1.5 weeks.

### G15 — UnifiedToolDescriptor primitive

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G15-01 | Define `UnifiedToolDescriptor(ConfigBase)` per `tool_descriptor_contract.md §2` | `nanobrain/core/tool.py` (extend, near `ToolConfig`) | — | M | All UTD fields defined; extra: forbid; load round-trips a UTD YAML |
| NB-G15-02 | Implement `ToolBase.from_descriptor(utd)` constructor | `nanobrain/core/tool.py` (extend ToolBase at l.50) | NB-G15-01 | M | Materializes a Tool from a UTD by deriving config from `provenance_pin`; calls `from_config` internally |
| NB-G15-03 | "Default minimal UTD" — introspect class for tools that don't supply one | `nanobrain/core/tool.py` | NB-G15-01 | S | Tool with no UTD gets a synthesized one (signature-derived inputs/outputs, docstring summary, R3 determinism, module-path id) |
| NB-G15-04 | UTD output-type → DataUnit-class mapping function | `nanobrain/core/tool.py` | NB-G15-01 | S | Per-type table: bytes → ProxyRef; small dict → Memory; file path → File |
| NB-G15-05 | Tests | `nanobrain/tests/core/test_utd.py` | NB-G15-04 | M | UTD load + roundtrip; from_descriptor + from_config equivalence; default-minimal computation; output-type mapping |

**Total for G15:** ~1.5 weeks. Unblocks Track C Rhea fork (UTD producer) and Track B (UTD consumer).

### G16 — ExecutionPlanConfig primitive

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G16-01 | Define `ExecutionPlanConfig(ConfigBase)` per `agent_workflow_authoring.md §3.1` | `nanobrain/library/orchestration/execution_plan.py` (new) | — | M | All ExecutionPlan fields; extra: forbid; JSON Schema export available |
| NB-G16-02 | Define `ExecutionPlanDataUnit(DataUnitMemory)` carrier | `nanobrain/library/orchestration/execution_plan.py` | NB-G16-01 | S | Subclass of DataUnitMemory; payload is `ExecutionPlanConfig`; serializes through the existing DataUnit machinery |
| NB-G16-03 | Tests | `nanobrain/tests/library/test_execution_plan.py` | NB-G16-02 | S | Schema round-trip; data unit set/get; integration with workflow YAML |

**Total for G16:** ~3 days.

### G17 — PlanLoweringStep + SkeletonLoaderStep built-ins

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G17-01 | Define `SkeletonLoaderStep(BaseStep)` — resolves `skeleton_id + version` against the registry | `nanobrain/library/orchestration/skeleton_loader_step.py` (new) | NB-G9-02, NB-G16-01 | M | `process()` resolves skeleton, returns its YAML body via a data unit |
| NB-G17-02 | Define `PlanLoweringStep(BaseStep)` — applies the 7 lowering steps from `agent_workflow_authoring.md §5` | `nanobrain/library/orchestration/plan_lowering_step.py` (new) | NB-G17-01, NB-G16-01, NB-G15-01 | L | Deterministic transformation: same plan + skeleton → same YAML bytes; rejects on Gate-2/3/5 violations |
| NB-G17-03 | `lowered_yaml_hash` computation in canonical form | `nanobrain/library/orchestration/plan_lowering_step.py` | NB-G17-02 | S | Sorted keys, normalized whitespace; SHA-256 of the canonical form |
| NB-G17-04 | Tests | `nanobrain/tests/library/test_plan_lowering.py` | NB-G17-03 | M | Determinism (same in → same out hash); each lowering sub-step's rejection path; round-trip with a real Track B skeleton |

**Total for G17:** ~2 weeks.

### G18 — LoopController + bounded-cycle relaxation

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G18-01 | Define `LoopController(BaseStep)` with `max_iterations`, `iteration_counter` data unit | `nanobrain/library/steps/loop_controller.py` (new) | — | M | Tracks iterations across re-entries; emits `loop_exhausted` signal at cap |
| NB-G18-02 | Workflow integrity validator: allow declared back-edges through a `LoopController`, reject undeclared cycles | `nanobrain/core/workflow.py` (extend cycle detector) | NB-G18-01 | M | A back-edge through a declared `LoopController` is not flagged as a cycle; back-edges through any other path still are |
| NB-G18-03 | Tests | `nanobrain/tests/library/test_loop_controller.py` | NB-G18-02 | M | Bounded loop completes; cap exhaustion routes to escalation path; non-controller back-edge rejected |

**Total for G18:** ~1 week.

### G19 — SignedConfig loader

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G19-01 | Define `SignedConfigLoader` extension to `from_config` path | `nanobrain/core/component_base.py` (extend) | — | M | Optional `--require-signed` flag; verifies detached `.sig` against operator's pubkey; FAIL-FASTs on mismatch |
| NB-G19-02 | Bundle exporter writes detached signature alongside `workflow.yml` | `apecx-mcp-integration/src/apecx_integration/execution/pbs_bundle.py` (Track B; Track A side is the loader only) | NB-G19-01 | S | (Cross-track: Track A ships loader; Track B ships exporter) |
| NB-G19-03 | Tests | `nanobrain/tests/core/test_signed_config.py` | NB-G19-01 | M | Valid signature: load succeeds; tampered: FAIL-FAST; missing sig + `--require-signed`: FAIL-FAST |

**Total for G19:** ~1 week.

### G20 — class: path import whitelist

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G20-01 | Define `ImportWhitelist(ConfigBase)` — allowlist of class-path prefixes | `nanobrain/core/component_base.py` (extend) | — | S | Whitelist loaded from operator config; default is "no whitelist" (current behavior) |
| NB-G20-02 | Wrap `from_config`'s class resolution to consult whitelist when set | `nanobrain/core/component_base.py` | NB-G20-01 | M | Class outside whitelist FAIL-FASTs with clear message |
| NB-G20-03 | Tests | `nanobrain/tests/core/test_import_whitelist.py` | NB-G20-02 | S | Whitelisted class loads; non-whitelisted FAIL-FASTs |

**Total for G20:** ~3 days.

### G21 — Detached / long-running Workflow.run_detached()

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G21-01 | Define `WorkflowRunnerConfig(ConfigBase)` + `WorkflowRunner` class skeleton | `nanobrain/library/runtime/workflow_runner.py` (new) | NB-G8-01 | M | from_config loads runner; placeholder run_detached returns task_id; persistence backend abstract |
| NB-G21-02 | Implement `PostgresTaskStore` + `SqliteTaskStore` durability backends | `nanobrain/library/runtime/task_store.py` (new) | NB-G21-01 | M | autonomous_task table CRUD; UNIQUE on task_id; heartbeat update API |
| NB-G21-03 | Implement `run_detached()` execution loop with heartbeat, cancellation, pause | `nanobrain/library/runtime/workflow_runner.py` | NB-G21-02, NB-G5-03 | L | A detached task survives caller exit; resumes from G5 checkpoint after process restart; honors operator pause/cancel |
| NB-G21-04 | Watchdog workflow (separate scheduled workflow that flags stale heartbeats) | `nanobrain/library/runtime/watchdog_workflow.yml` (new) | NB-G21-02 | S | Stale-heartbeat detection: tasks transition to `failed: heartbeat_lost` after 10 min |
| NB-G21-05 | Tests + integration test | `nanobrain/tests/integration/test_workflow_runner.py` | NB-G21-04 | M | Detached round-trip; restart-resume; cancel + pause + heartbeat cycles all verified |

**Total for G21:** ~2 weeks. Required by Track B autonomy sub-track.

### G22 — External-event trigger primitives wired into orchestration

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| NB-G22-01 | Define `WorkflowEntryTriggerConfig(ConfigBase)` + `WorkflowEntryTrigger` adapter | `nanobrain/library/runtime/workflow_entry_trigger.py` (new) | NB-G21-01 | M | from_config loads trigger; on inner-trigger fire, calls `Workflow.run_detached()` with payload from `payload_factory` |
| NB-G22-02 | Wire existing `TimerTrigger` and `ManualTrigger` through `WorkflowEntryTrigger` (existing primitives at `nanobrain/core/trigger.py:1277` and `:1416`) | `nanobrain/library/runtime/workflow_entry_trigger.py` | NB-G22-01 | S | Cron + interval + queue-row triggers all start a fresh workflow run via the runner |
| NB-G22-03 | Implement new `EventTrigger` — HTTP webhook + message-bus subscription primitive | `nanobrain/core/trigger.py` (extend; new class after `ManualTrigger` at l.1416) | NB-G1-02 (predicate DSL for event filtering) | L | Webhook variant: HTTP POST handler; message-bus variant: Redis Pub/Sub or Kafka subscriber; event_filter uses G1 predicate DSL |
| NB-G22-04 | Tests | `nanobrain/tests/integration/test_workflow_entry_trigger.py`, `nanobrain/tests/integration/test_event_trigger.py` | NB-G22-03 | M | All three trigger types start the meta-workflow; missed-schedule policy honored; webhook authentication path covered |

**Total for G22:** ~1.5 weeks.

---

## 3. Track B — apecx-mcp-integration implementation

Track B builds the orchestrator, the Tier-1/2/3 agents, the skeleton library,
and the control-plane extensions. Each Track B task can either consume the
nanobrain primitive (when the corresponding Track A task is done) or
implement a workaround (per `development_roadmap.md` per-phase tables).

Tasks are prefixed `MC-<area>-<NN>`.

### Phase 0 — Foundation Layer (matches `development_roadmap.md §3`)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-P0-01 | `DataAccessInterface` abstract base class | `apecx-mcp-integration/src/apecx_integration/agents/data_access/__init__.py` (new) + `data_access_interface.py` | — | M | ABC with `query`, `fetch_record`, `list_sources` methods; mockable in tests |
| MC-P0-02 | `SphericalAdapter` concrete implementation | `apecx-mcp-integration/src/apecx_integration/agents/data_access/spherical_adapter.py` (new) | MC-P0-01 | L | Implements DataAccessInterface against Spherical REST API; integration test against real endpoint |
| MC-P0-03 | `GlobusAdapter` stub | `apecx-mcp-integration/src/apecx_integration/agents/data_access/globus_adapter.py` (new) | MC-P0-01 | S | Interface-compliant; logs calls; returns empty results |
| MC-P0-04 | `IntentClassifier` LLM-based router | `apecx-mcp-integration/src/apecx_integration/composition/intent_classifier.py` (new) | — | M | Single LLM call; maps query to one of 5 orchestrator intents; integration test with Ollama |
| MC-P0-05 | `GenericQueryOrchestrator` (migrates current 23-tool behavior) | `apecx-mcp-integration/src/apecx_integration/agents/orchestrators/generic.py` (new) | MC-P0-01, MC-P0-04 | L | Single-source lookups bypass full multi-agent path; matches current behavior |
| MC-P0-06 | Unify three current entity-resolution implementations into single `CanonicalEntityResolver` | `apecx-mcp-integration/src/apecx_integration/synonym_dictionary/canonical_resolver.py` (new) + delete duplicates | MC-P0-01 | M | One `resolve(surface_form, entity_type)` API; behavior parity with current three implementations under integration test |
| MC-P0-07 | `ask` MCP tool | `apecx-mcp-integration/src/apecx_integration/mcp_surface/tools/ask.py` (new); register in `server.py` | MC-P0-04, MC-P0-05 | M | Returns task_id (async pattern); integration test through Claude Desktop loop |
| MC-P0-08 | `status` MCP tool | `apecx-mcp-integration/src/apecx_integration/mcp_surface/tools/status.py` (new) | MC-P0-07 | S | Polls or streams task progress; backed by control plane Run state |
| MC-P0-09 | Phase 0 end-to-end integration test | `apecx-mcp-integration/tests/integration/test_phase0_e2e.py` (new) | MC-P0-08 | M | Real query → ask → status → grounded answer in <60s; recorded transcript in PR |

**Phase 0 dependencies on Track A:** None. Phase 0 ships on existing nanobrain primitives.

### Phase 1 — Full Retrieval Layer (matches `development_roadmap.md §4`)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-P1-01 | `RetrievalAgent` base + per-source subclasses (PubMed, Globus, FAISS, BioactivityDB, ProtaBank, InteractionDB, PDB, EMDB, GenomicsDB, DomainDB) | `apecx-mcp-integration/src/apecx_integration/agents/retrieval/` (new dir, 1 file per source) | MC-P0-01 | XL | Each agent produces typed evidence dict per `multiagent_architecture.md §6.2`; per-source integration test |
| MC-P1-02 | Extended `EvidenceAccumulationStep` (inherits from existing `SynthesisContextAssemblyStep`) | `apecx-mcp-integration/src/apecx_integration/composition/steps/evidence_accumulation.py` (new) | MC-P1-01 | M | Aggregates outputs from all active retrieval agents; branch-failure → empty bundle (existing pattern) |
| MC-P1-03 | `CrossSourceIntegrationStep` | `apecx-mcp-integration/src/apecx_integration/composition/steps/cross_source_integration.py` (new) | MC-P1-02, MC-P0-06 | M | Per `workflow_output_contract.md §5.2`: entity resolution + concordance scoring across sources |
| MC-P1-04 | `BioTargetOrchestrator` (Tier-1) | `apecx-mcp-integration/src/apecx_integration/agents/orchestrators/biotarget.py` (new) | MC-P1-03 | L | Per `multiagent_architecture.md §5.2`; parallel fan-out to retrieval agents; evidence accumulation; synthesis |
| MC-P1-05 | `StructuralOrchestrator` (Tier-1) | `apecx-mcp-integration/src/apecx_integration/agents/orchestrators/structural.py` (new) | MC-P1-04 | M | Subset of BioTargetOrchestrator focused on PDB + EMDB |
| MC-P1-06 | Typed `LayerResult` schema (uses G6 when shipped; hand-rolled BaseModel until then) | `apecx-mcp-integration/src/apecx_integration/composition/schemas/layer_result.py` (new) | (NB-G6-02 OR workaround) | S | Pydantic model with extra: forbid; layer steps return instances |
| MC-P1-07 | Phase 1 end-to-end integration test | `apecx-mcp-integration/tests/integration/test_phase1_e2e.py` (new) | MC-P1-06 | M | Multi-source query produces FinalResponse with findings from ≥4 sources; canonical IDs cross-referenced |

**Phase 1 dependencies on Track A:** G6 (typed LayerResult) — workaround acceptable.

### Phase 2 — Layered Reasoning Workflow (matches `development_roadmap.md §5`)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-P2-01 | `Phase0PlanningStep` — produces ExecutionPlan | `apecx-mcp-integration/src/apecx_integration/composition/steps/phase0_planning.py` (new) | MC-P0-04, (NB-G16-02 OR workaround) | M | Consumes user query + session_context; emits ExecutionPlanDataUnit (or workaround DataUnitMemory wrapping plan dict) |
| MC-P2-02 | `SequenceLayerStep`, `StructuralLayerStep`, `FunctionalLayerStep`, `EvidenceLiteratureLayerStep`, `CrossSourceLayerStep`, `DesignLayerStep` | `apecx-mcp-integration/src/apecx_integration/composition/steps/layers/` (new dir, 6 files) | MC-P1-01 | L | Each consumes ExecutionPlan + retrieval-agent output; produces typed LayerResult |
| MC-P2-03 | `ResponseSynthesisStep` (extends existing `RagSynthesisStep`) | `apecx-mcp-integration/src/apecx_integration/composition/steps/response_synthesis.py` (new) | MC-P2-02, MC-P1-02 | M | Per `workflow_output_contract.md §6`; grounding gate enforced |
| MC-P2-04 | `FollowupGenerationStep` | `apecx-mcp-integration/src/apecx_integration/composition/steps/followup_generation.py` (new) | MC-P2-03 | M | Per `workflow_output_contract.md §8`; 3 data-grounded follow-ups |
| MC-P2-05 | Session state store (`SessionContext` persisted in control plane) | `apecx-mcp-integration/src/apecx_integration/control_plane/models/session.py` (new) + alembic migration | — | M | Per `workflow_output_contract.md §9`; CRUD via control plane API; turn-by-turn append |
| MC-P2-06 | `LayeredReasoningWorkflow` YAML — full DAG with ConditionalLinks | `apecx-mcp-integration/src/apecx_integration/composition/workflows/layered_reasoning/workflow.yml` (new) | MC-P2-04, MC-P2-05 | L | Loadable through `Workflow.from_config`; uses declarative G1 predicates if shipped, else hand-authored callables |
| MC-P2-07 | Workaround: "publish empty bundle" sentinel for inactive layers (until G2 ships) | every layer step | MC-P2-02 | M | Each layer step writes empty `LayerResult` when gated; remove when G2 lands |
| MC-P2-08 | Workaround: lint rule rejecting any DirectLink without explicit `auto_transfer: true` (until G7 ships) | `apecx-mcp-integration/scripts/lint_workflow_yamls.py` (new) | — | S | CI fails on a YAML missing the flag |
| MC-P2-09 | Phase 2 two-turn integration test (session reuse) | `apecx-mcp-integration/tests/integration/test_phase2_e2e.py` (new) | MC-P2-06 | M | Turn 2 follow-up reuses Turn 1 evidence without re-retrieving |

**Phase 2 dependencies on Track A:** G1 + G2 + G7 + G10 + G14 + G16 (workarounds in MC-P2-07/08, hand-rolled BaseModel + DataUnitMemory).

### Phase 3 — External Tool Execution (matches `development_roadmap.md §6`)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-P3-01 | `ToolExecutionStep` (apecx-mcp local; promote when G11 ships) | `apecx-mcp-integration/src/apecx_integration/composition/steps/tool_execution.py` (new) | (NB-G11-01 OR workaround), MC-P3-04 | M | Consumes a UTD reference; dispatches to Rhea backend (Track C); typed output |
| MC-P3-02 | `RheaToolAgent` adapter | `apecx-mcp-integration/src/apecx_integration/agents/tool_execution/rhea_agent.py` (new) | MC-P3-01, T-RH-04 (Track C UTD endpoint) | L | HTTP+SSE client; ProxyStore key-based I/O; integration test against real Rhea fork |
| MC-P3-03 | UTD catalog client + cache | `apecx-mcp-integration/src/apecx_integration/composition/tools/utd_catalog.py` (new) | (NB-G15-01 OR hand-rolled UTD), T-RH-03 (Track C catalog endpoint) | M | Caches UTDs by `descriptor_hash`; refreshes per `discovery_cache_ttl_seconds` |
| MC-P3-04 | ProxyStore configuration + workspace integration | `apecx-mcp-integration/src/apecx_integration/composition/proxystore_config.py` (new) | (NB-G3-02 OR workaround), (NB-G13-01 OR run_id prefixing in apecx-mcp) | M | Connects to Redis configured via env; per-run namespacing applied at all key writes |
| MC-P3-05 | Update `LayerStep`s to invoke `ToolExecutionStep` (replace P2 no-op stubs with real calls) | `apecx-mcp-integration/src/apecx_integration/composition/steps/layers/*.py` | MC-P3-02, MC-P2-02 | M | Layer-step tool calls flow through ToolExecutionStep; integration test against Rhea muscle/clustalw |
| MC-P3-06 | Phase 3 end-to-end integration test (real tool execution) | `apecx-mcp-integration/tests/integration/test_phase3_e2e.py` (new) | MC-P3-05 | M | Real workflow with 1+ tool invocation produces correct answer; provenance records present |

**Phase 3 dependencies on Track A:** G3 + G4 + G11 + G13 + G15 — workarounds available.
**Phase 3 dependencies on Track C:** T-RH-04 (Rhea fork UTD endpoint).

### Phase 4 — Hypothesis Tournament + HPC Bundles (matches `development_roadmap.md §7`)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-P4-01 | `HypothesisTournamentStep` | `apecx-mcp-integration/src/apecx_integration/composition/steps/hypothesis_tournament.py` (new) | MC-P3-05, (NB-G18-01 OR custom back-edge) | L | Per `reasoning_patterns_library.md P2`; parallel proposers + evidence-scored ranking |
| MC-P4-02 | Skeleton library — initial 5 skeletons (rag_e2e_synthesis, multi_source_discovery, hypothesis_tournament, structural_analysis, single_db_lookup) | `apecx-mcp-integration/src/apecx_integration/composition/skeletons/` (new dir, 5 subdirs each with skeleton.yml + skeleton.schema.json) | (NB-G9-01 OR hand-rolled skeleton machinery) | XL | Each skeleton loads through `Workflow.from_config` (after hole substitution); validates against its schema |
| MC-P4-03 | `PlanLoweringStep` (apecx-mcp local; promote when G17 ships) | `apecx-mcp-integration/src/apecx_integration/composition/orchestration/plan_lowering.py` (new) | MC-P4-02, (NB-G17-02 OR local impl) | L | Implements the 7 lowering steps from `agent_workflow_authoring.md §5`; deterministic hash |
| MC-P4-04 | 5-gate validation pipeline as BaseStep chain | `apecx-mcp-integration/src/apecx_integration/composition/orchestration/validation_gates/` (new dir, 5 step files + linker workflow.yml) | MC-P4-03 | L | Each gate is a BaseStep; chain wired by ConditionalLinks; structured rejections feed repair loop |
| MC-P4-05 | Repair LoopController + back-edge | `apecx-mcp-integration/src/apecx_integration/composition/orchestration/repair_loop.py` (new) | MC-P4-04, (NB-G18-01 OR custom) | M | Two-attempt cap per `agent_workflow_authoring.md §7`; escalation path on third failure |
| MC-P4-06 | Bundle exporter v2 — content-addressed snapshots, signed config (G19 producer side) | `apecx-mcp-integration/src/apecx_integration/execution/pbs_bundle.py` (extend existing) | (NB-G19-01 OR detached-sig only), (NB-G12-03 OR hand-rolled aggregation) | M | Bundle layout matches `hpc_reproducibility_spec.md §4`; replay protocol passes round-trip test |
| MC-P4-07 | Phase 4 end-to-end integration test | `apecx-mcp-integration/tests/integration/test_phase4_e2e.py` (new) | MC-P4-06 | M | Tournament + HITL + HPC bundle export + re-ingest round-trip works on a real Rhea workflow |

**Phase 4 dependencies on Track A:** G5 + G9 + G12 + G17 + G18 + G19 — workarounds available, but Track B owns more without them.

### Phase 5 — Globus Migration + Production Hardening (matches `development_roadmap.md §8`)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-P5-01 | `GlobusAdapter` full implementation | `apecx-mcp-integration/src/apecx_integration/agents/data_access/globus_adapter.py` (replace stub) | MC-P0-03 + Globus endpoint testable | L | All Phase 1 integration tests pass with `APECX_DB_BACKEND=globus`; no retrieval-agent change |
| MC-P5-02 | Remove `SphericalAdapter` once Globus is stable | `apecx-mcp-integration/src/apecx_integration/agents/data_access/` | MC-P5-01 | S | Spherical-specific code deleted; tests pass on Globus only |
| MC-P5-03 | Production class-path whitelist (G20 consumer side) | `apecx-mcp-integration/src/apecx_integration/composition/composer.py` (extend); `_configs/production_whitelist.yml` (new) | (NB-G20-01 OR wrapping loader) | M | Production-mode workflow loads reject any class outside the whitelist |
| MC-P5-04 | Replace `Workflow.process()` + `wait_for_cascade()` patterns with `Workflow.run()` | `apecx-mcp-integration/src/apecx_integration/control_plane/executors/local.py` | NB-G8-01 | S | Call sites simplified; behavior unchanged |
| MC-P5-05 | Remove all G-workarounds once each gap ships (drive WORKAROUND_INVENTORY.md to zero) | `apecx-mcp-integration/src/apecx_integration/composition/` (multiple files) | All NB-G* tasks complete | M | No remaining `# G-workaround` comments; lint rule from MC-P2-08 deleted (now redundant with G7) |

**Phase 5 dependencies on Track A:** G8 + G20 — workarounds available; full unwind requires all 20 gaps shipped.

### Cross-phase apecx-mcp tasks (not phase-bound)

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-X-01 | Workaround inventory tracker — `WORKAROUND_INVENTORY.md` | `apecx-mcp-integration/docs/WORKAROUND_INVENTORY.md` (new) | — | S | One row per workaround: gap_id, file, removal trigger task |
| MC-X-02 | Migrate `composer_prompts/system.md` to PromptTemplate carriers (G14 consumer) | `apecx-mcp-integration/src/apecx_integration/composition/composer_prompts/` (refactor to subdirs each with template.yml) | NB-G14-04 | M | Composer reads through PromptTemplateManager; provenance records carry template_id+content_hash |
| MC-X-03 | Migrate orchestrator framing — adopt `meta_workflow_orchestration.md` topology | `apecx-mcp-integration/src/apecx_integration/composition/composer.py` (refactor) | NB-G16-02, NB-G17-02 | XL | Composer becomes a thin shim over the meta-workflow Workflow; orchestrator-as-workflow demonstrated |
| MC-X-04 | Skeleton publish workflow (`skeleton_library_publish_workflow.yml`) | `apecx-mcp-integration/src/apecx_integration/composition/workflows/skeleton_publish/` (new) | MC-P4-02 | M | Per `data_layer_evolution.md §4`; runs Gates 1-4 in dry-run before publish |
| MC-X-05 | Prompt template publish workflow | `apecx-mcp-integration/src/apecx_integration/composition/workflows/prompt_publish/` (new) | NB-G14-03, MC-X-02 | M | Per `data_layer_evolution.md §4`; runs regression tests before publish |
| MC-X-06 | Data-source registry (apecx-mcp) | `apecx-mcp-integration/src/apecx_integration/control_plane/models/data_source_registry.py` (new) + alembic migration | — | M | Per `data_layer_evolution.md §13`: SQLite table; UNIQUE constraint on `(source_name, version_hash)` |
| MC-X-07 | Lifecycle workflows (FAISS rebuild, taxdump refresh, dictionary build, etc.) | `apecx-mcp-integration/src/apecx_integration/composition/workflows/lifecycle/` (new dir, ~7 workflow YAMLs) | MC-X-06 | L | Each workflow per `data_layer_evolution.md §4`; integration test for at least one publish path |

### Autonomy sub-track (per `autonomous_workflow_agent.md`)

These tasks ship the autonomous orchestrator service that consumes Track A
G21 + G22. The autonomous orchestrator REUSES the meta-workflow's step graph
(per `meta_workflow_orchestration.md §7.5`) — only entry trigger and runtime
context differ. No new orchestrator code; only runtime + control-plane +
MCP-tools work.

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| MC-AU-01 | `autonomous_task` + `autonomous_task_run` tables (Postgres + alembic migration) | `apecx-mcp-integration/src/apecx_integration/control_plane/models/autonomous_task.py` (new) + `_alembic/versions/<id>_autonomous_tasks.py` | — | M | Schema per `autonomous_workflow_agent.md §5`; CRUD via control plane API; UNIQUE on task_id |
| MC-AU-02 | Autonomous orchestrator service entry point (`apecx-cp serve --role autonomous`) | `apecx-mcp-integration/src/apecx_integration/control_plane/cli.py` (extend) + `apecx-mcp-integration/src/apecx_integration/control_plane/runtime/autonomous_service.py` (new) | NB-G21-01, MC-AU-01 | L | Service starts a `WorkflowRunner` with PostgresTaskStore; honors operator commands; heartbeats |
| MC-AU-03 | Autonomy capability flags + max-level enforcement | `apecx-mcp-integration/src/apecx_integration/composition/composer_config.yml` (extend) + `apecx-mcp-integration/src/apecx_integration/control_plane/gates/autonomy_capability_check.py` (new) | NB-G22-01 | M | `composer.allow_autonomous` + `composer.max_autonomy_level` honored; tasks exceeding ceiling rejected at trigger time |
| MC-AU-04 | Five autonomy MCP tools (`start_autonomous_task`, `list_autonomous_tasks`, `pause_autonomous_task`, `cancel_autonomous_task`, `show_autonomous_audit`) | `apecx-mcp-integration/src/apecx_integration/mcp_surface/tools/autonomous.py` (new); register in `server.py` | MC-AU-02 | L | Per `mcp_surface.md` §autonomous-task tools; conditional registration on `composer.allow_autonomous: true` |
| MC-AU-05 | Cost envelope enforcement + near-exhaustion deferred-HITL | `apecx-mcp-integration/src/apecx_integration/control_plane/accounting/autonomous_envelope.py` (new) | MC-AU-02 | M | Per `autonomous_workflow_agent.md §8`; per-task and per-deployment-per-day caps; near-exhaustion request fires at 80% |
| MC-AU-06 | Deferred-HITL fields on existing Approval model + per-gate auto-resolution policy | `apecx-mcp-integration/src/apecx_integration/control_plane/models/approval.py` (extend) + `apecx-mcp-integration/src/apecx_integration/control_plane/gates/auto_resolution.py` (new) | MC-AU-02 | M | `task_id` + `deferred_hitl` fields added; per-gate behavior matrix (per `hitl_safety_gates.md §3.1.1`) honored at timeout |

**Total for autonomy sub-track:** ~6 weeks one-engineer. Deliverable is a
shippable `apecx-cp serve --role autonomous` service plus the user-facing
MCP tools.

---

## 4. Track C — Rhea fork

The Rhea fork lives outside the current workspace; **task T-RH-00 is to create
the fork and add it as a sibling under `apecx-cowork/`**. All subsequent Track C
tasks assume the fork is checked out at `apecx-cowork/rhea/`.

We are free to modify the fork (per the user's note), so Track C is unblocked
in a way GalaxyMCP is not. The Rhea fork's job is to produce UTDs that Track A's
G15 + Track B's MC-P3-02/03 consume, and to cooperate with Track A's G3 + G4
+ G13 (ProxyStore namespace, provenance records).

Tasks are prefixed `T-RH-<NN>`.

| ID | Task | Files | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| T-RH-00 | Fork upstream Rhea (https://github.com/chrisagrams/rhea); check out at `apecx-cowork/rhea/`; commit baseline | (new repo) | — | S | Clean checkout; baseline tests pass; CI green on the fork's main branch |
| T-RH-01 | Add `apecx_utd_extension` module to the fork | `rhea/rhea/extensions/apecx_utd_extension/` (new) | T-RH-00 | S | Module loads; no behavior change to baseline endpoints |
| T-RH-02 | Define UTD producer for Rhea's tool catalog — emit a UTD per registered tool | `rhea/rhea/extensions/apecx_utd_extension/utd_producer.py` (new) | T-RH-01, NB-G15-01 (UTD schema) | M | Every tool in Rhea's catalog has a derived UTD; matches `tool_descriptor_contract.md §2` schema |
| T-RH-03 | UTD discovery endpoint — `GET /apecx/utds` returns catalog of UTDs | `rhea/rhea/server/routes/apecx_utd.py` (new); register in app | T-RH-02 | M | Endpoint returns paginated UTD list; supports `?descriptor_id=` query; OpenAPI doc auto-generated |
| T-RH-04 | UTD-aware invocation — `POST /apecx/invoke` accepts a UTD reference + bindings; returns ProxyStore keys | `rhea/rhea/server/routes/apecx_utd.py` (extend) | T-RH-03 | M | Invocation matches existing Rhea `/invoke` semantics but takes UTD instead of tool_name; SSE stream preserved |
| T-RH-05 | ProxyStore namespace cooperation — accept caller-provided `run_id` and prefix all returned keys with `run_<run_id>/` | `rhea/rhea/proxystore/namespace.py` (new) | T-RH-04, NB-G13-01 (run_id contract) | M | A request with `X-Apecx-Run-Id: <uuid>` header gets keys prefixed accordingly; integration test against Track B caller |
| T-RH-06 | Provenance record cooperation — emit per-invocation provenance JSONL matching `external_tool_integration.md §6.2` schema | `rhea/rhea/extensions/apecx_utd_extension/provenance.py` (new) | T-RH-04 | M | Every invocation writes a JSONL line with the 12 required fields per spec; sink configurable per request |
| T-RH-07 | Self-published UTD overlay — Rhea's tool catalog can be augmented with apecx-side UTD overlays for tools we register | `rhea/rhea/extensions/apecx_utd_extension/overlay_loader.py` (new) | T-RH-02 | M | Operator config points at an overlay directory; each overlay file is a UTD that Rhea registers as a virtual tool |
| T-RH-08 | Configure fork-side CI to run UTD-extension tests | `rhea/.github/workflows/utd_tests.yml` (new) | T-RH-01 | S | UTD tests run on every PR; integration test (against a local Rhea instance) gated on Docker availability |
| T-RH-09 | Fork-side integration test — caller invokes a tool via UTD; receives ProxyStore keys; reads them back | `rhea/tests/integration/test_apecx_utd_e2e.py` (new) | T-RH-07 | M | End-to-end: UTD discovery → invocation → key resolution → provenance check; smoke + real-Redis variants |

**Track C deferral:** Promotion of UTD-extension to mainline Rhea is out of scope —
the fork is the integration boundary. We may upstream later.

---

## 5. Track D — Cross-track integration

Track D tasks combine artifacts from Tracks A, B, and C into shippable
end-to-end behavior. They are the actual shipping milestones — every Track D
task ends with a recorded run against real data per the workspace mocks
carve-out rule.

Tasks are prefixed `XT-<NN>`.

| ID | Task | Files / artifacts | Depends on | Effort | DoD |
|---|---|---|---|---|---|
| XT-01 | End-to-end smoke: Phase 0 ask → grounded answer | New CI job | MC-P0-09 | S | Real query produces grounded answer; CI passes daily |
| XT-02 | End-to-end multi-source: Phase 1 cross-source query | New CI job | MC-P1-07 | S | 4-source query produces FinalResponse with cross-source canonical IDs |
| XT-03 | End-to-end layered reasoning: Phase 2 two-turn session | New CI job | MC-P2-09 | M | Two-turn integration test green on CI; session-reuse demonstrated |
| XT-04 | End-to-end tool execution: Phase 3 real Rhea tool call | New CI job + Track C dependency | MC-P3-06, T-RH-09 | M | Real Rhea-fork tool invocation produces correct answer; provenance + ProxyStore verified |
| XT-05 | End-to-end HPC bundle round-trip on real Polaris/Aurora | Operator-driven (not CI) | MC-P4-06 | L | Bundle exported, qsub'd, results round-tripped back via `/hpc/ingest`; replay protocol verifies hashes |
| XT-06 | End-to-end Globus migration: Phase 5 swap test | New CI job | MC-P5-01 | M | Same Phase 1 tests pass with `APECX_DB_BACKEND=globus` |
| XT-07 | End-to-end signed-bundle replay | New CI job | MC-P4-06, NB-G19-01 | M | Bundle with detached signature replays under `--require-signed`; tampered bundle FAIL-FASTs |
| XT-08 | End-to-end whitelist enforcement | New CI job | MC-P5-03, NB-G20-01 | S | Workflow with non-whitelisted class FAIL-FASTs in production mode; allowed class loads |
| XT-09 | Workaround-removal verification — drive `WORKAROUND_INVENTORY.md` to zero | Cross-cutting; per workaround removal | MC-P5-05 | M | Each workaround removal is a separate PR with the corresponding gap-ship reference |
| XT-10 | Production deployment dry-run | Operator-driven | XT-04 + XT-05 + XT-08 | L | Full system deployed to staging cluster per `deployment_architecture.md` H mode; smoke-tests pass |
| XT-11 | End-to-end autonomous task with deferred-HITL round-trip | New CI job + Track B autonomy sub-track | MC-AU-04 + MC-AU-06 + NB-G22-04 | M | Schedule-triggered autonomous task pauses for HITL via deferred-HITL request; user responds via Claude Desktop's `approve` MCP tool; task resumes and completes; audit log shows full transition record |

---

## 6. Critical path

The shortest path from "current state" to "Phase 4 + HPC bundle round-trip
on real Rhea" cuts across all tracks. Critical-path nodes:

```
T-RH-00 (fork creation, S)
  → T-RH-01 (UTD extension scaffold, S)
NB-G15-01 (UTD primitive in nanobrain, M)
  → T-RH-02 (UTD producer in fork, M)
  → T-RH-03 (UTD discovery endpoint, M)
  → T-RH-04 (UTD invoke endpoint, M)
NB-G3-02 (DataUnitProxyRef, L)
  → T-RH-05 (ProxyStore namespace cooperation, M)
NB-G16-02 (ExecutionPlanDataUnit, S) + NB-G17-02 (PlanLoweringStep, L)
  → MC-P4-03 (PlanLoweringStep adoption, L)
  → MC-P4-04 (5-gate validation chain, L)
  → MC-P4-05 (repair loop, M)
MC-P4-02 (skeleton library, XL) [parallel to NB tasks]
MC-P4-06 (bundle exporter v2, M)
  → XT-04 (real Rhea tool call, M)
  → XT-05 (HPC bundle round-trip, L)
```

**Autonomous-agent critical sub-path** (separate from the Phase 4 critical
path; can run in parallel after Phase 2 ships):

```
NB-G21-01..05 (WorkflowRunner + persistence + watchdog, ~2 weeks)
NB-G22-01..04 (WorkflowEntryTrigger + EventTrigger, ~1.5 weeks) [parallel to G21]
  → MC-AU-01 (autonomous_task tables, M)
  → MC-AU-02 (autonomous service entry, L)
  → MC-AU-03 (capability flags, M)
  → MC-AU-04 (five autonomy MCP tools, L)
  → MC-AU-05 (cost envelope enforcement, M)
  → MC-AU-06 (deferred-HITL fields + auto-resolution, M)
  → XT-11 (e2e autonomous task with deferred-HITL round-trip, M)
```

Aggregate Phase 4 critical-path effort: ~3-4 calendar months for one team
running A and C in parallel, B following A by ~2 weeks. Autonomous-agent
sub-path adds ~6 additional weeks if shipped after Phase 2; ~0 additional
weeks if Track A G21+G22 are paralleled with the Phase-2 G1+G2+G7+G14
sprint (autonomy MCP tools (MC-AU-04) and the e2e test (XT-11) are still
serialized).

**Parallel acceleration opportunities:**
- Track C T-RH-00 → T-RH-04 can run before NB-G15 if the fork ships an interim UTD using the spec from `tool_descriptor_contract.md §2` directly (without depending on the nanobrain primitive).
- Track B Phase 0 + Phase 1 are independent of Tracks A and C and can ship before any framework gap.
- Track A G1 + G2 + G7 + G14 (Phase 2 dependencies) can be done in parallel by different engineers; only the integration test (Track B MC-P2-09) gates on all four.
- Track A G21 + G22 (autonomous-agent dependencies) are independent of Phase-2 gaps and can run in parallel — the only true serialization is MC-AU-* depending on G21+G22 being done.

---

## 7. Effort and resource summary

| Track | Total tasks | Aggregate effort | Critical resource |
|---|---|---|---|
| A — nanobrain | 87 (across G1–G22) | ~25.5 weeks one-engineer | Framework engineer fluent in nanobrain internals |
| B — apecx-mcp | 57 (across phases + cross-phase + autonomy sub-track) | ~30 weeks one-engineer | Application engineer + LLM/Ollama for integration tests |
| C — Rhea fork | 10 | ~3.5 weeks one-engineer | Tool-platform engineer + Rhea/Parsl familiarity |
| D — Cross-track | 11 | ~5.5 weeks (mostly setup + recurring) | Project lead + on-call ops for HPC dry-runs |
| **Total** | **165 tasks** | **~64.5 weeks one-engineer / ~16-17 weeks 4-engineer team** | |

These numbers are aggressive single-engineer estimates and ignore the
"two heads beat one head" effect for design clarification, code review, and
debugging. Realistic 4-engineer team: ~5-6 calendar months end-to-end.

Brutal-truth caveats:
- Effort estimates are uncertainty-budgeted, not pessimism-padded. Every L is
  a judgment call with ±50% variance.
- The skeleton library (MC-P4-02) is the single largest task; its XL
  estimate assumes 5 skeletons each with a hand-validated YAML and schema.
  If we need more or the skeletons are more complex, this slips.
- HPC dry-run (XT-05, XT-10) effort is hard to predict because it depends on
  cluster scheduling, allocation availability, and operator rotation.

---

## 8. Definition of "task complete"

A task is complete when ALL of the following hold:

1. **Code lands.** All files listed in the task's `Files` column are committed.
2. **Tests pass.** Both unit (where listed) and integration tests cited in DoD.
   No mocks for behaviors verified by integration tests (per workspace
   mocks carve-out rule).
3. **Recorded verification.** The exact test command + abbreviated output
   appears in the commit body or PR description.
4. **Cross-references updated.** If the task ships a primitive that other
   docs reference as forthcoming, the doc is updated in the same PR (e.g.,
   when NB-G1-05 lands, `nanobrain_workflow_design.md §3.1` removes the
   "until G1 ships" caveat).
5. **Workaround retired.** When a Track A gap ships and a Track B workaround
   becomes redundant, the workaround removal is a follow-up PR cited in the
   gap-shipping PR's body. Tracking lives in `WORKAROUND_INVENTORY.md`.
6. **Reviewer sign-off.** Per `apecx-mcp-integration/.claude/agents/review-gate.md`
   pre-commit checklist passes.

A task is NOT complete if:
- Tests are skipped (`pytest --ignore=`) without a recorded justification (per
  CLAUDE.md rule on skipping tests).
- Mocks substitute for integration tests (per CLAUDE.md mocks carve-out).
- The integration test was run against synthetic data instead of real data.
- The PR description claims behavior that the test output does not demonstrate.

---

## 9. How to use this graph

**For an implementer picking up a task:**
1. Find the task by ID. Read the `Files`, `Depends on`, and `DoD` columns.
2. Verify dependencies are complete by checking the referenced task IDs are
   marked done in the project tracker.
3. Implement, test, and submit with the DoD checklist annotated in the PR.

**For a project lead sequencing work:**
1. Use §6 (critical path) to identify the longest-pole sequence.
2. Use §3-§5 to assign tasks to engineers; tasks within a track that share
   no dependencies can be parallelized to the team's headcount.
3. Use the per-phase dependency tables in `development_roadmap.md` to know
   when a phase can start (any required gap shipped OR workaround accepted).

**For a reviewer auditing a PR:**
1. Find the task ID(s) the PR claims to complete. Cross-check against §3-§5
   to confirm all DoD items are addressed.
2. Verify the PR body cites the test command and output (item 3 in §8).
3. If the PR retires a workaround, confirm the gap-shipping task is also done.

**For an operator planning a deployment:**
1. Track D tasks (XT-*) are the milestones. Each XT task is a deployable
   capability.
2. `deployment_architecture.md` describes the runtime topology each XT
   task ends in (L / C / H).

---

## 10. Cross-references

| Resource | Location | Used here for |
|---|---|---|
| Master design index | `docs/_design_index.md` | Map task IDs back to design docs |
| Nanobrain capability gaps catalog | `docs/nanobrain_capability_gaps.md` | Source of G1–G20 specs each Track A task implements |
| Development roadmap | `docs/development_roadmap.md` | Phase structure that Track B tasks slot into |
| Agent workflow authoring | `docs/agent_workflow_authoring.md` | Source of the lowering pipeline + 5-gate spec (MC-P4-03/04) |
| Tool descriptor contract | `docs/tool_descriptor_contract.md` | UTD schema implemented by NB-G15-01 + T-RH-02 |
| HPC reproducibility spec | `docs/hpc_reproducibility_spec.md` | Bundle layout (MC-P4-06) + replay protocol (XT-05/07) |
| Data layer evolution | `docs/data_layer_evolution.md` | Lifecycle workflows (MC-X-04/05/06/07) |
| Deployment architecture | `docs/deployment_architecture.md` | Runtime topology (XT-10) |
| Security threat model | `docs/security_threat_model.md` | Whitelist enforcement (MC-P5-03 + XT-08); signed config (MC-P4-06 + XT-07) |
| Workspace policy | `../CLAUDE.md` | Mocks carve-out, three-attempt rule, integration-test discipline |
| nanobrain framework rules | `../nanobrain/CLAUDE.md` | from_config-only, process()-not-execute(), no hardcoded prompts |

---

## 11. Open questions specific to the task graph

1. **Rhea fork ownership.** Who owns the apecx fork after T-RH-00? Is upstream
   sync planned (regular merges from `chrisagrams/rhea`), or do we
   permanently diverge once we add the UTD extension?

2. **Skeleton authoring scope.** §3 MC-P4-02 ships 5 skeletons. Is that the
   whole catalog, or are domain-specific skeletons (e.g., one per scientific
   subdomain) planned? If yes, that XL becomes XXL.

3. **Test data availability.** Several integration tests (XT-04 in particular)
   require a real Rhea instance with at least one tool registered. Who runs
   that instance? Is it part of CI (Docker-Compose'd in a job) or operator-managed?

4. **Effort estimate calibration.** The team has not yet shipped a Track A
   task end-to-end. After the first 2-3 NB-G* tasks land, the estimates here
   should be re-calibrated against actual cycle time.

5. **Nanobrain release cadence.** The deprecation WARNING from G7 needs ≥1
   nanobrain release of bake time before the v2 default flips. If the
   project ships nanobrain on a 2-month cadence, that's a hard 2-month gap
   between landing G7 step 1 and step 4.

6. **Workaround inventory drift.** MC-X-01 introduces an inventory tracker.
   If workarounds accumulate faster than they retire, the inventory becomes
   debt rather than a checklist. Triage cadence: monthly review?

---

## 12. Maintenance protocol

This document is **alive**. When any of the following happens, this document
is updated in the same PR (not a follow-up):

- A task ID's scope changes — update the task description; add a `superseded_by:`
  reference if the task is split.
- A Track A primitive ships — update the corresponding Track B / Track C
  tasks to remove the workaround language.
- A new gap is added to `nanobrain_capability_gaps.md` — add the corresponding
  Track A task block here.
- A new doc is added to the design package — add it to §10 cross-references.
- An effort estimate is empirically wrong by >2x — update it; note the source.

If this document drifts from the actual state of the codebase, the
implementer who notices the drift fixes it in their PR. We do not maintain
this graph in a separate cadence from the code.
