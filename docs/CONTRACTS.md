# Design Contracts

Each section answers a specific contract cited from operational
source-code docstrings. Anchors (e.g., `#g7`, `#decision-p6+a`) are
stable — keep them when editing or you'll dangle the pointer.

For "what's where" navigation, see `_design_index.md`.

---

## Framework capability gaps (G1-G22, G44)

The "gaps" are framework primitives that were missing when the
apecx-mcp integration started; each one is now shipped, with a
docstring-cited contract here. Severity: **P0** (silent-failure or
adoption-blocker), **P1** (workaround in consumer until shipped),
**P2** (hygiene / security hardening).

### #g1 — Declarative `ConditionalLink` predicate DSL — P0

**Issue.** Pre-G1, `ConditionalLink` accepted "any Python callable"
as the predicate. The callable was referenced by import path or
lambda string in YAML; either forced the authoring agent to
synthesize *Python source* and a *correct import path*. LLMs
hallucinate either. Cannot be validated at workflow load. Cannot be
inspected by the bundle exporter.

**Contract.** YAML carries `predicate: {op, field, value}` where
`op ∈ {eq, ne, in, contains, exists}` and `field` is a dotted path;
combinators `predicate: {op: all|any|not, of: [<predicate>, ...]}`.
The predicate evaluates against the link's `source:` data unit
payload. Fixed vocabulary — additions require a framework version
bump.

**Anti-pattern.** Don't author lambda-string predicates; the v2
config schema FAIL-FASTs on legacy callable predicates.

**Lives in.** `nanobrain/core/link.py` (`PredicateConfig`,
`evaluate_predicate`, `get_nested_value_strict`).

### #g2 — `AllDataReceivedTrigger` dynamic configuration — P0

**Issue.** Pre-G2, AllDataReceived was static — the fan-in key set
was baked at workflow load time. A workflow with conditional layers
that fan into a synthesis step needed dynamic configuration.

**Contract.** `AllDataReceivedTrigger.expected_keys` is configurable
via a path-reference at load OR dynamically by writing to a
configured DataUnit. Triggers fire when every expected key has
arrived AND the trigger's `gate_semantics` rule (#g10) is satisfied.

**Lives in.** `nanobrain/core/trigger.py`.

### #g3 — `DataUnitProxyRef` for ProxyStore-backed values — P0

**Issue.** Pre-G3, every DataUnit held its value in-process. Passing
multi-GB payloads between steps via DataUnitMemory was infeasible.

**Contract.** `DataUnitProxyRef` holds a ProxyStore Key instead of
the value. `.get()` materializes via the registered Store; `.set()`
PUTs and stores the returned Key. Cross-process: the manifest
carries connector hints (file path / redis host:port) so a fresh
ResumeStep can re-register the Store.

**Lives in.** `nanobrain/core/data_unit.py:DataUnitProxyRef`.

### #g4 — Step-level provenance threading — P1

**Issue.** Pre-G4, `process()` ran with no automatic provenance
recording. Recording was per-step opt-in, often forgotten.

**Contract.** `BaseStep._execute_process` wraps every `process()`
call: resolves `current_provenance_context()`, records inputs /
outputs / exception / timing on success AND on raise. Recorder
failures are swallowed (logged WARNING) so a buggy recorder cannot
mask the step's real exception. When no context is active,
recording is a fast no-op.

**Lives in.** `nanobrain/core/step.py:_execute_process`,
`nanobrain/core/provenance.py`.

### #g5 — `CheckpointStep` + `ResumeStep` — P1

**Issue.** Pre-G5, no framework primitive for snapshotting a
partial workflow run + re-entering it. HPC bundle replay and
deferred-HITL composition both needed this.

**Contract.** `CheckpointStep.process(input)` snapshots every
captured key to filesystem (default) or ProxyStore. Manifest JSON
records `manifest_version`, `step_name`, captured keys, entries,
`code_identity`. Atomic write (tmp + rename). `ResumeStep` reads the
manifest, restores values, verifies `content_hash`. `on_missing`:
`fail | skip | rebuild`.

**Anti-pattern.** Don't try to snapshot stream-shaped values
(`__aiter__`); FAIL-FASTs at capture time.

**Lives in.** `nanobrain/library/steps/checkpoint_resume.py`.

### #g6 — Typed result schemas at the framework boundary — P0

**Issue.** Pre-G6, `process()` returns were typed only by docstring.
A step that returned the wrong shape was caught only when downstream
unwrapping failed — far from the source.

**Contract.** `StepConfig.step_input_schema` and `step_output_schema`
declare Pydantic models. The framework validates the dict at the
wire boundary on every `process()` invocation. Both default `None`
(no validation) for backwards compatibility.

**Lives in.** `nanobrain/core/step.py:_execute_process`.

### #g7 — `auto_transfer` default flip — P0 (behavior-changing)

**Issue.** Pre-G7, every `LinkConfig` defaulted `auto_transfer=False`.
Without explicit `auto_transfer: true` in YAML, the link silently
no-opped — workflow loaded, every step ran, no exception, but no
data ever transferred. **The dominant silent-failure shape.**

**Contract.** `config_version: 2` flips the default to True. v2
mutator injects `auto_transfer: true` on omitted-keys; explicit
values preserved via `setdefault`. v2 also rewrites path-reference
configs (`config: "external.yml"` link entries).

**Anti-pattern.** Don't author `LinkConfig` YAML without
`auto_transfer: true` declared, even under v2 — the mutator catches
it but the explicit declaration is more readable.

**Lives in.** `nanobrain/core/workflow.py:WorkflowConfig._apply_v2_link_defaults`.

### #g8 — `Workflow.run()` await semantics — P0

**Issue.** `Workflow.process()` is fire-and-forget (data-driven
mode): it writes to the first step's input data unit and returns a
status dict immediately while the cascade runs in background. Tests
and synchronous callers need a way to await completion.

**Contract.** `Workflow.run(payload, timeout=..., settle_ms=...)`
delegates to `wait_for_cascade` after `process()`. Returns the
workflow's output data unit values once the cascade drains. The
status dict is observability metadata; the cascade is the data-flow
mechanism. Workflow's `_update_output_data_units` overrides the base
class to skip writing the status dict into output units (silent-
failure prevention).

**Lives in.** `nanobrain/core/workflow.py:run`, `wait_for_cascade`,
`_update_output_data_units`.

### #g9 — `Workflow.from_skeleton(skeleton, bindings)` — P0

**Issue.** Pre-G9, the composer's Phase 1 (skeleton lowering)
required a manual `PlanLoweringStep + SkeletonLoaderStep` dance.
Each composer-emitted plan had to chain those steps.

**Contract.** `Workflow.from_skeleton(skeleton, bindings)` collapses
the dance: loads the skeleton (path / dict / Skeleton), validates
bindings (FAIL-FAST on missing/extras), substitutes
`{{name: type}}` tokens, materializes lowered YAML to a temp file,
delegates to `from_config`. See #workflow-skeleton-holes for the
hole grammar + #workflow-lowering for the 7-step pipeline.

**Lives in.** `nanobrain/core/workflow.py:from_skeleton`,
`nanobrain/library/orchestration/skeleton.py`.

### #g10 — Gate-to-bottom semantics — P0

**Issue.** A `ConditionalLink` that evaluated False against a
fan-in target left the target's `AllDataReceivedTrigger` waiting
forever for the missing key — workflow deadlocked.

**Contract.** `LinkConfig.gate_semantics` (and
`TriggerConfig.gate_semantics`): `"publish_empty"` (legacy default)
emits a sentinel; `"gate_to_bottom"` writes
`__nanobrain_gated_off__` to the target. `AllDataReceivedTrigger`'s
`_is_satisfied(payload)` predicate counts the sentinel as
satisfied AND excludes it from the trigger payload — fan-in
proceeds with N-1 keys instead of deadlocking. User `process()`
never sees the magic string.

**Lives in.** `nanobrain/core/link.py:ConditionalLink`,
`nanobrain/core/trigger.py:AllDataReceivedTrigger`.

### #g11 — Tool-step taxonomy / `LocalParslAdapter` — P1

**Issue.** Pre-G11, tool dispatch was step-class-specific (each
tool was a custom Step). No reusable adapter pattern.

**Contract.** `ToolExecutionStep` + `ToolBackendAdapter` protocol.
`BackendKind` literal (#td-vocab) selects the adapter.
`LocalParslAdapter` (`BACKEND_NAME="local_parsl"`) dispatches
Python callables via Parsl with `executor_kind="thread"` default
(#decision-p0++b).

**Lives in.** `nanobrain/library/steps/tool_execution_step.py`,
`nanobrain/library/tools/local_parsl_adapter.py`.

### #g12 — Declarative `ResourceEnvelope` on a Step — P1

**Issue.** Pre-G12, resource requirements (CPU, memory, GPU) were
implicit. The HPC bundle exporter had no way to query a Step's
resource needs.

**Contract.** `StepConfig.resource_envelope` declares
`cpu_cores`, `memory_gb`, `gpu_count`, `wall_time_seconds`. Fields
are optional. The bundle exporter aggregates child step envelopes
into a workflow-level submission spec.

**Lives in.** `nanobrain/core/step.py`,
`nanobrain/core/workflow.py:aggregate_resource_envelopes`.

### #g13 — Multi-tenant ProxyStore namespacing — P1

**Issue.** Pre-G13, ProxyStore Keys were global. Two workflow runs
sharing a Store could collide.

**Contract.** `WorkflowRunContext` carries `run_namespace`; every
DataUnitProxyRef writes Keys under that namespace. Nested workflows
derive their namespace from the outer via `derive_nested_namespace`
honoring `WorkflowConfig.namespace_strategy: "scoped" | "inherit"`
(default scoped; multi-tenant safety > over-isolation).

**Lives in.** `nanobrain/library/orchestration/run_context.py`,
`nanobrain/core/workflow.py:namespace_strategy`,
`nanobrain/core/data_unit.py:DataUnitProxyRef`.

### #g14 — `PromptTemplate` primitive — P1

**Issue.** Pre-G14, prompts were stored as Python strings, mixed
with code, no declared holes, no regression fixtures.

**Contract.** `PromptTemplate` declares typed holes (see
#prompt-template-hole-grammar). YAML stores the template body +
hole declarations + `regression_fixtures` (input/output pairs the
testing harness can verify). Substitution validates types +
constraints at runtime.

**Lives in.** `nanobrain/core/prompt_template_manager.py`.

### #g15 — `UnifiedToolDescriptor` primitive — P0

**Issue.** Pre-G15, tool metadata was scattered: name in code,
schema in pydantic, capability requirements in docstrings, backend
choice in YAML. No single source of truth.

**Contract.** `UnifiedToolDescriptor` (UTD) is a Pydantic record
with fixed-vocabulary literals (#td-vocab): `BackendKind`,
`DeterminismClass`, `Resource`, plus input/output schema +
`requires_capability` (#td-capability) + cost-estimate hooks.
`ToolBase.from_python_callable` auto-derives a UTD from a callable's
signature + docstring + type annotations.

**Lives in.** `nanobrain/core/unified_tool_descriptor.py`.

### #g16 — `ExecutionPlanConfig` primitive — P0

**Issue.** Pre-G16, the composer's Phase 0 output (the execution
plan) was an ad-hoc dict.

**Contract.** `ExecutionPlanConfig` is the Pydantic schema of the
Phase 0 output (see #workflow-execution-plan). Strategy literal
matches Strategy A/B (#workflow-strategies). Provenance fields
threaded through every layer. The dataclasses live in
`nanobrain/library/orchestration/execution_plan.py`.

### #g17 — `PlanLoweringStep` + `SkeletonLoaderStep` built-ins — P0

**Issue.** Pre-G17, every consumer of the lowering pipeline had to
implement it. The pipeline is the same across consumers.

**Contract.** Two built-in steps cover the lowering pipeline (see
#workflow-lowering). `SkeletonLoaderStep` runs Step 1 (resolve +
validate skeleton); `PlanLoweringStep` runs Steps 2-7
(substitute + mutate + validate + serialize). Either can be
composed manually OR via `Workflow.from_skeleton` (#g9).

**Lives in.** `nanobrain/library/orchestration/plan_lowering_step.py`,
`skeleton_loader_step.py`.

### #g18 — `LoopController` step + bounded-cycle relaxation — P1

**Issue.** The framework's DAG validator rejects cycles. But a
reasoner-validator loop with structured-feedback retry
(#reasoning-p7) IS a (bounded) cycle.

**Contract.** `LoopController` step. Steps marked
`bounded_loop: true` are exempt from cycle detection. The
controller enforces `max_iters`; beyond the bound, the loop emits
a failure event and the workflow terminates via downstream
`ConditionalLink` on the loop's success-output.

**Lives in.** `nanobrain/library/steps/loop_controller.py`.

### #g19 — `SignedConfig` loader option — P2

See #td-signing.

### #g20 — `class:` path import whitelist — P2

See #threat-t-cl-1.

### #g21 — `Workflow.run_detached()` — P1

**Issue.** Pre-G21, every workflow ran synchronously. The autonomy
mode's "schedule it, walk away, check back" pattern was impossible.

**Contract.** `WorkflowRunner.run_detached(workflow_callable,
task_id, payload)` schedules an asyncio task and returns the queued
`DetachedTaskHandle` immediately. Two task store backends:
`in_memory` (default) and `sqlite` (stdlib). Lifecycle states:
`queued → running → (completed | cancelled | failed | suspended)`.
`pause` / `resume` are cooperative via `PauseSignal` contextvar;
`cancel(task_id)` cooperatively cancels the asyncio task. Heartbeat
watchdog reaps stale tasks (#autonomy-heartbeat).

The Postgres backend (`PostgresTaskStore`) ships as a lazy/optional
import for cross-process resume.

**Lives in.** `nanobrain/library/runtime/workflow_runner.py`,
`nanobrain/library/runtime/task_store.py`.

### #g22 — External-event trigger primitives — P1

**Issue.** Pre-G22, the runtime had no way to launch a workflow
from an external event (webhook, message bus, scheduled time).

**Contract.** `EventTrigger` (transport-agnostic; callers invoke
`fire_event(body)` from a webhook handler / consumer).
`TimerTrigger` (recurring schedule with missed-schedule policy
`skip | catch_up | merge`). `WorkflowEntryTrigger` wraps any inner
trigger to launch a detached workflow run via G21's
`WorkflowRunner`; auto-generates task IDs; optionally calls a
dotted-path-resolved `payload_factory`. Durable inner-trigger →
launch binding via `EntryStateStore` (in-memory + file-backed) so
fires aren't lost across process restarts.

**Lives in.** `nanobrain/core/trigger.py:EventTrigger`,
`nanobrain/library/runtime/entry_triggers.py`.

### #g44 — `DataUnitProxyRef.namespace()` strict mode — P0

**Issue.** Pre-G44, `DataUnitProxyRef.namespace()` returned `""`
silently when no `WorkflowRunContext` was active. Multi-tenant
isolation was an illusion — keys landed in the global namespace.

**Contract.** `DataUnitProxyRef.namespace()` emits a rate-limited
WARNING per instance when no context is active, OR raises
`ComponentConfigurationError` under
`NANOBRAIN_STRICT_NAMESPACE=1`. The strict mode is the
silent-failure-prevention surface.

**Lives in.** `nanobrain/core/data_unit.py:DataUnitProxyRef`.

---

## Decision records (eval_03 chain)

These are commitments — operator-overridable surfaces are documented
on each. Source: 2026-05-09 → 2026-05-11 implementation chain.

### #decision-p0+a — workspace root + log dir resolution

**Decision.** `_default_writable_log_dir()` resolves in this order:
`$NANOBRAIN_LOG_DIR` → `~/.cache/nanobrain/logs/` → tempdir.
Cwd-relative `Path("logs")` is retired (G33). Workspace root
resolution honors `$APECX_WORKSPACE_ROOT` env first, then walks
upward looking for markers (`pyproject.toml`, `.git`, `setup.py`,
`CLAUDE.md`, `apecx-mcp-integration`); returns the CLOSEST matching
ancestor (G40).

**Override.** Set the env var.

**Lives in.** `nanobrain/core/async_logging.py`,
`nanobrain/core/logging_system.py`,
`nanobrain/library/runtime/workspace_root.py`.

### #decision-p0++b — Parsl preset

**Decision.** `LocalParslAdapter` defaults `executor_kind="thread"`
(ThreadPoolExecutor — lowest overhead, fork-safe, no cluster
prereqs).

**Override.** `LocalParslAdapter.from_config(..., executor_kind=
"process"|"htex"|"thread")`. Bring-your-own `parsl_config` for HPC.

**Lives in.** `nanobrain/library/tools/local_parsl_adapter.py`.

### #decision-p4+b — capability-token transport

**Decision.** `WorkflowRunContext` is the SINGLE source of truth for
`capability_tokens`. No env-var fallback, no per-step config carve-out.

**Override.** None (intentional — multiple transports = silent-failure
shape).

**Lives in.** `nanobrain/core/capabilities.py`.

### #decision-p4+c — step-event schema versioning

**Decision.** v1 frozen with explicit `event_schema_version: int` on
every emitted event. Additive fields bump the version; semantic
changes keep both versions for at least one minor release.

**Lives in.** `nanobrain/core/step_events.py`.

### #decision-p6+a — deferred-HITL approval ID strategy

**Decision.** `DeferredHITLStep` defaults
`approval_id_strategy="deterministic"`: SHA-256 of `(run_id,
step_name, prompt)`. Retries hit the same record; no duplicate
approvals.

**Override.** `approval_id_strategy="random"` (uuid4 per call —
test-only).

**Lives in.** `nanobrain/library/steps/deferred_hitl_step.py`.

### #decision-p6+b — cost cap granularity

**Decision.** Per-step cap AND per-workflow cap, both declarative,
both optional. Per-workflow cap is the OUTER (cumulative across every
step's `record()`); per-step caps live on StepConfig and check at
step boundary.

**Override.** Omit the cap dimension on `CostEnvelope` to disable.
Absent = no cap on that dimension.

**Lives in.** `nanobrain/core/cost_envelope.py`.

### #decision-p6+c — data-source registry manifest format

**Decision.** YAML. `DataSourceEntry.content_hash` is `sha256:<hex>`;
helper `compute_content_hash` hashes files OR directories
order-stably. Unknown entry keys FAIL-FAST (typo protection).

**Lives in.** `nanobrain/library/runtime/data_source_registry.py`.

---

## Tool descriptor contract

UTD is a typed, framework-internal record of a tool's interface +
determinism + capability constraints. Cited from
`nanobrain/core/unified_tool_descriptor.py` and
`nanobrain/core/capabilities.py` and `nanobrain/core/signed_config.py`.

### #td-vocab — fixed vocabulary (G15)

**Contract.** The UTD literal types are fixed:

- `BackendKind`: `"local_python" | "local_parsl" | "http" | "rhea_mcp"`
- `DeterminismClass`: `"strict" | "approximate" | "stochastic"`
- `Resource`: `"cpu_only" | "gpu_single" | "gpu_multi" | "filesystem_local" | "network"`

Custom literals are NOT allowed without a framework version bump and
a sweep of every UTD in the catalog. This is by design — the fixed
surface is what the authoring agent (and the human reviewer) can hold
in one prompt.

### #td-capability — capability tokens

**Contract.** Tool dispatch consults `utd.requires_capability` before
the adapter is touched. The active `WorkflowRunContext` must carry a
matching capability token in `capability_tokens` or
`CapabilityNotGranted` raises (workflow-terminal).

Tokens are opaque strings issued by the runner; the framework does
not interpret them. No env-var fallback (#decision-p4+b).

**Lives in.** `nanobrain/core/capabilities.py:verify_capability`,
`nanobrain/library/steps/tool_execution_step.py`.

### #td-signing — detached signatures (G19)

**Contract.** `SignedConfig` loader option verifies an ed25519
signature alongside the config payload. Failure to verify is
load-time FAIL-FAST.

**Algorithm.** ed25519 (PEP 8032). The key fingerprint is part of the
signature record so a future algorithm rotation can ship without
breaking existing signatures.

**Lives in.** `nanobrain/core/signed_config.py`.

---

## HITL safety gates

Cited from `nanobrain/core/capabilities.py` and
`nanobrain/core/cost_envelope.py`.

### #hitl-gate-cap — capability-gated tool dispatch

**Contract.** A tool whose UTD declares
`requires_capability="<token>"` MUST NOT execute unless the active
run-context's `capability_tokens` includes that token. The check
happens BEFORE the adapter is invoked (no partial side effects).

**Lives in.** `nanobrain/library/steps/tool_execution_step.py`.

### #hitl-gate-cost-r1 — runtime cost halt

**Contract.** When a `CostTracker.record(kind, amount)` call would
push the cumulative past the declared cap, the tracker raises
`CostEnvelopeBreach`. The runner treats this as a terminal failure
with `reason="cost_envelope_breach"`.

NaN and ±inf reject up-front with `ValueError` (silent-failure
prevention — NaN comparisons always return False so NaN slips past
both `< 0` AND `> cap` without the explicit guard).

**Lives in.** `nanobrain/core/cost_envelope.py`.

---

## Security threat model

Cited from `nanobrain/core/import_whitelist.py` and
`nanobrain/core/signed_config.py`.

### #threat-t-cl-1 — class-loading via YAML

**Threat.** Composer-emitted YAML carries `class:` paths that the
framework imports at load time. An attacker who can influence the
composer can place arbitrary import paths; the framework would
import-and-construct them.

**Mitigation.** Two-stage whitelist:
- Stage 1 (composer-side, AST scanner before emit) — see
  `apecx-mcp-integration/docs/whitelist_layering.md`.
- Stage 2 (framework-side, `nanobrain.core.import_whitelist`) — the
  whitelist of importable class paths is configured at framework
  init; loading a class outside it raises
  `ComponentConfigurationError`.

The stages cover different bypass classes; folding them into one is
out of scope. Source: `whitelist_layering.md`.

**Lives in.** `nanobrain/core/import_whitelist.py`.

### #threat-signed-configs — supply-chain signing

See #td-signing.

---

## Autonomous workflow agent

Cited from `nanobrain/library/runtime/workflow_runner.py`,
`nanobrain/library/steps/deferred_hitl_step.py`,
`nanobrain/core/cost_envelope.py`.

### #autonomy-heartbeat — heartbeat-watchdog defaults

**Contract.** `WorkflowRunner` heartbeat defaults:
`heartbeat_interval_seconds=30`, `stale_task_seconds=180`. A task
that hasn't heartbeated within `stale_task_seconds` is reaped + its
status flipped to `failed` with `reason="heartbeat_stale"`.

**Override.** Pass kwargs at `from_config` time; setting
`heartbeat_interval_seconds=0` disables.

**Lives in.** `nanobrain/library/runtime/workflow_runner.py`.

### #autonomy-hitl — pause-for-human

**Contract.** `DeferredHITLStep` is the framework primitive: emit an
approval, raise `ApprovalPendingError`, return the decision on
re-entry. The step is idempotent + stateless; the
`ApprovalStore` holds all state. Suspension is signaled by the
exception; resumption is just re-running. See
`nanobrain/docs/g27_g21_wiring_design.md` for the runner-side
wiring (Option A + opt-in Option B).

**Lives in.** `nanobrain/library/steps/deferred_hitl_step.py`.

### #autonomy-cost — per-deployment cost ceiling

**Contract.** A `CostEnvelope` declared at workflow level is the
OUTER cap (cumulative across every step's `record()` call). When
breached, `CostEnvelopeBreach` raises and the runner treats it as a
terminal failure. The autonomy mode's "halt the runaway task" loop
relies on this — without the framework primitive, the
per-deployment ceiling cannot stop a single task. See
#hitl-gate-cost-r1.

---

## HPC reproducibility

Cited from `nanobrain/core/unified_tool_descriptor.py`,
`nanobrain/library/tools/python_callable_dispatcher.py`.

### #hpc-determinism — three-tier determinism

**Contract.** Every UTD declares `determinism_class`:

- `"strict"` — same inputs ⇒ same outputs, bit-for-bit. Pure
  Python, no randomness, no wall-time, no network.
- `"approximate"` — same inputs ⇒ outputs within a documented
  tolerance. Numerical algorithms with stable convergence.
- `"stochastic"` — same inputs ⇒ different outputs each call. LLMs,
  Monte Carlo, randomized algorithms.

The HPC bundle replay mechanism only re-runs `"strict"` and
`"approximate"` tools deterministically; `"stochastic"` outputs are
captured-and-replayed from the bundle rather than re-computed.

**Anti-pattern.** Don't classify an LLM call as `"strict"` just
because the prompt is fixed. Temperature=0 doesn't make the
server-side model deterministic across versions.

---

## LLM prompt contracts

Cited from `nanobrain/core/prompt_template_manager.py`,
`nanobrain/library/testing/prompt_regression.py`.

### #prompt-ac1-regressions — observed regressions

**Contract.** Two AC1-breaking regressions are pinned as hard rules
in the composer system prompt (`composition/composer_prompts/system.md`):

1. **No `TransformLink`.** LLMs hallucinate `transform_function`
   import paths. Use `DirectLink` + novel Python when shape-bridging.
2. **Path-reference `config:` for library components.** Inline
   `config: {...}` forces the LLM to reproduce `input_data_units`
   / `output_data_units` / `triggers` blocks and hallucinate their
   class paths.

If AC1 starts flapping, check this file BEFORE blaming the LLM.

### #prompt-template-hole-grammar — PromptTemplate holes

**Contract.** A `PromptTemplate` declares typed holes:
`{name: type[, constraint]}`. Types: `str | list[str] | int | enum
("a"|"b") | json`. Validation runs at template-load AND at
substitution time; missing/extra holes FAIL-FAST.

The hole grammar matches the skeleton hole grammar in
#workflow-skeleton-holes (G9).

**Lives in.** `nanobrain/core/prompt_template_manager.py`.

---

## Agent workflow authoring

Cited from `nanobrain/library/orchestration/*.py`.

### #workflow-strategies — strategy A vs B (§2.1)

**Contract.** Two composition strategies:

- **A — skeleton + bindings.** Compose by binding typed holes in a
  fixed skeleton (`Workflow.from_skeleton(skeleton, bindings)`).
  Best for parameterized workflows where the topology is fixed.
- **B — free composition.** Hand-author the workflow YAML directly,
  or programmatically via `nanobrain.lightweight.WorkflowBuilder`.

Strategy A is the agent-friendly path; Strategy B is the
human-friendly path. The framework supports both.

### #workflow-execution-plan — plan + provenance (§3 / §3.1)

**Contract.** `ExecutionPlan` is the Phase 0 output of the layered-
reasoning workflow: a typed dict declaring `active_layers`,
`layer_options`, plus per-field provenance (which agent / which
prompt / which retrieved evidence justified each decision).
Provenance is threaded through every downstream layer via
`ConditionalLink` gating on `active_layers`.

The plan dataclasses live in
`nanobrain/library/orchestration/execution_plan.py`; the field
semantics are pinned here.

### #workflow-skeleton-holes — skeleton hole grammar (§4 / §4.1)

**Contract.** A skeleton declares typed holes:
`{{name: type[, default]}}`. Substitution happens at
`Workflow.from_skeleton()` time. Each hole has:
- A type (`str | list[str] | int | enum(...) | json`)
- An optional default
- An optional constraint (regex, range, enum membership)

Missing required holes FAIL-FAST at substitution time. Extra hole
bindings FAIL-FAST too (typo protection).

Skeleton directory layout: `<name>.skeleton.yml` + optional
`<name>.tests.yml` (regression fixtures).

**Lives in.** `nanobrain/library/orchestration/skeleton.py`.

### #workflow-lowering — plan lowering (§5)

**Contract.** `PlanLoweringStep` + `SkeletonLoaderStep` together
implement the 7-step lowering pipeline:

1. Resolve the skeleton (load + validate).
2. Validate the binding set (G9-completion).
3. Substitute holes into the YAML.
4. Run the v2 mutators (auto_transfer defaults, path-reference
   rewriting).
5. Validate the resulting workflow (`WorkflowValidator`).
6. Pin the provenance (which plan, which bindings, which skeleton
   version).
7. Serialize to a temp file for `Workflow.from_config`.

`Workflow.from_skeleton()` collapses steps 1-7 into one call.

**Lives in.** `nanobrain/library/orchestration/plan_lowering_step.py`,
`skeleton_loader_step.py`.

### #workflow-gates — validation gates (§6)

**Contract.** The lowering pipeline runs four validation gates:

- **Gate 1: skeleton load.** Must parse + every hole declared has a
  type.
- **Gate 2: binding shape.** Every required hole bound; no extras.
- **Gate 3: post-substitution YAML validity.** The substituted YAML
  parses as `WorkflowConfig`.
- **Gate 4: graph validity.** DAG, no cycles, every Link's
  source+target reference a real step's data unit.

Each gate's failure surface is documented in the corresponding step
class.

### #workflow-repair-loop — loop controller (§7)

**Contract.** `LoopController` step + bounded-cycle relaxation.
A reasoner produces a draft → a validator rejects → the controller
feeds the rejection back into the reasoner → repeat up to `max_iters`
times. The bounded cycle is allowed in the DAG (cycle-detection
relaxes for steps marked `bounded_loop: true`).

See #reasoning-p7 for the retry-with-feedback pattern.

**Lives in.** `nanobrain/library/steps/loop_controller.py`.

---

## Reasoning patterns

### #reasoning-p7 — retry with structured feedback

**Pattern.** When a reasoner's output fails validation, the rejection
is structured (e.g., `{"failure_type": "missing_citation",
"offending_token": "[Globus xyz]"}`) and fed BACK to the reasoner
in the next iteration as part of the prompt context. The reasoner
sees its own previous output + the structured failure.

Cycle bounded by `max_iters` (default 3). Beyond the bound, the
step emits a failure event and the workflow terminates the loop
(downstream `ConditionalLink` on the loop's success-output gates).

**Lives in.** `nanobrain/library/steps/loop_controller.py`.

---

## Workflow output contract

Cited from `nanobrain/library/orchestration/execution_plan.py`.

### #output-layer — Layer pair (§3.2)

**Contract.** A `Layer` pairs `layer_type` with `LayerResult`. The
`layer_type` is a fixed literal — see #output-layer-types. Every
layer that fires in a workflow run emits exactly one `LayerResult`;
the workflow output is `list[Layer]`.

### #output-layer-types — fixed layer-type vocabulary (§4.1)

**Contract.** `layer_type` literals:
`"structural" | "evolutionary" | "molecular" | "literature" | "synthesis"`.

Additions require a framework version bump. New layer kinds belong
in a NEW step + a new literal added in lockstep; do not subtype
existing layers.

---

## Data layer

### #data-version-pin (§3-4)

**Contract.** Data source consumers consult `DataSourceRegistry` for
canonical `(source_id, version)` pairs + content hashes. The
registry rejects unknown entry keys at load time (typo protection)
and FAIL-FASTs with `ContentHashMismatch` when a consumed file's
hash diverges from the registry. Operators bump the registry entry
explicitly when re-pinning to a new version.

The pre-G24 era had each consumer rolling its own version pin policy;
this is now centralized.

**Lives in.** `nanobrain/library/runtime/data_source_registry.py`.

---

## External tool integration

### #ext-tool-dispatch — adapter contract

**Contract.** `ToolExecutionStep` dispatches to an adapter chosen by
the UTD's `BackendKind` literal (#td-vocab). Each adapter implements
`ToolBackendAdapter.execute(utd, payload, context) -> result`. The
framework does NOT instantiate adapters speculatively — only when a
UTD points at one. `ProxyStore` reference-based I/O is the canonical
model for HPC-scale payloads: pass a Key, the adapter materializes
the value when it actually needs it.

Failure-degradation table per backend lives in the adapter's
docstring. The framework's only contract is: errors raise; the runner
classifies + the workflow terminates.

**Lives in.** `nanobrain/library/steps/tool_execution_step.py`,
`nanobrain/library/tools/`.

---

## Architecture brutal-truth list

### #arch-brutal-#3 — auto_transfer silent-failure

**Brutal truth.** Until config_version: 2 ships, every hand-authored
LinkConfig YAML MUST include `auto_transfer: true`. Without it, the
link silently no-ops: the workflow loads, every step runs, no
exception, but no data ever transfers. See G7.

The G7 default-flip in v2 closes this. Until then: lint via
`apecx-mcp-integration/scripts/lint_workflow_yamls.py`.

---

## Alignment audit findings

### #alignment-§4.2 — orchestrator design

**Finding.** The orchestrator pattern (`PlanLoweringStep` +
`SkeletonLoaderStep` + `LoopController` + composer prompt) is the
framework-native answer to "how does an agent compose a workflow
without hallucinating Python source." The earlier patterns
(`TransformLink`, inline `config:` dicts) led to AC1 regressions
(see #prompt-ac1-regressions).

**Lives in.** `nanobrain/library/orchestration/`.

---

## Mock audit findings

### #mock-§2-c1 — aiohttp MCP client mock

**Audit.** Pre-fix, `nanobrain/core/mcp_support.py` and
`a2a_support.py` silently fell back to a mock MCP client when
`aiohttp` was not importable. This made tests pass while production
failed. The mock branch now emits a WARNING log line so operators
cannot miss it. Production code paths MUST verify aiohttp before
relying on the client.

**Lives in.** `nanobrain/core/mcp_support.py`,
`nanobrain/core/a2a_support.py`.

### #mock-§2-d1 — config-loader env interpolation

**Audit.** Pre-fix, the config loader silently substituted empty
strings for missing env vars during `${VAR}` interpolation. This
caused workflows to "load successfully" with empty credentials. The
loader now distinguishes `${VAR:-default}` (allowed) from `${VAR}`
(required); the latter FAIL-FASTs when VAR is unset.

**Lives in.** `nanobrain/core/config/config_manager.py`.
