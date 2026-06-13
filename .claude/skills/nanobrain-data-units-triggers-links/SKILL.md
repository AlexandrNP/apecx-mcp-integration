---
name: nanobrain-data-units-triggers-links
description: The event-driven contract — how DataUnits emit change events, how Triggers subscribe (or poll), and how Links transfer data from one step's output to another step's input. Read whenever you wire steps together, define triggers, or add a link. Covers all DataUnit subclasses (Memory/File/String/Stream/object-ref), all Trigger types (DataUnitChange/AllDataReceived/Timer/Manual), all Link types (Direct/Queue/Transform/Conditional/File/Academy), the AsyncTriggerExecutor's deadlock prevention, and the verbatim error messages.
---

# nanobrain-data-units-triggers-links

## ✅ Silent-failure cures shipped (2026-05-09)

The two dominant silent-failure shapes (`auto_transfer` and the
gate-deadlock) are **CURED BY DEFAULT** as of 2026-05-09. New
workflows opting into v2 (which is now the default) get safe
semantics automatically. Legacy v1 callers are warned and remain
free to keep the old behavior by declaring `config_version: 1`
explicitly.

### auto_transfer no-op (G7) — CURED BY v2 DEFAULT

Historical bug: every `DirectLink` (and `TransformLink` /
`ConditionalLink` / `FileLink`) silently no-ops without
`auto_transfer: true`; downstream consumers see empty inputs; the
workflow appears to "work."

**Cure status:**
- **G7 Step 1+2** — v1 workflows that omit `auto_transfer` get a
  deprecation WARNING at workflow load.
- **G7 Step 3** — `config_version: 2` injects `auto_transfer: true`
  into every inline link config. Explicit values are NEVER overridden.
- **G7 Step 4 (2026-05-09)** — `config_version` defaults to **2**.
  Path-reference link configs (`config: "external.yml"`) are loaded,
  injected, and rewritten in-memory to nested-inline form so the
  v2 mutator applies uniformly across both inline and path-reference
  shapes. Authors who want the legacy False default MUST declare
  `config_version: 1` explicitly.

**Detection signal:** still useful for legacy v1 workflows. The
workflow completes "successfully" but downstream data units hold
their initial value (often `None` or empty list/dict). Run
`Workflow.wait_for_cascade()` synchronously after `process()`; if
no state change at downstream units, declare `config_version: 2`
or add `auto_transfer: true` explicitly.

### Gate-deadlock (G10) — CURED BY WORKFLOW-LEVEL gate_semantics

Historical bug: a `ConditionalLink` whose predicate evaluates False
under the legacy `gate_semantics: publish_empty` is a no-op; a
downstream `AllDataReceivedTrigger` waits indefinitely for the
gated-off branch.

**Cure status:**
- **G10 Step 1** — per-link / per-trigger `gate_semantics:
  gate_to_bottom`. The gated branch writes
  `ConditionalLink.GATED_OFF_SENTINEL`; the trigger counts the
  sentinel as satisfied and excludes it from the trigger payload
  (user `process()` never sees the magic string).
- **G10 Step 2 (2026-05-09)** — workflow-level `gate_semantics`
  propagation. Set `gate_semantics: gate_to_bottom` once at the
  WorkflowConfig level and the framework stamps it on every inline
  ConditionalLink AND every inline AllDataReceivedTrigger
  (workflow-level + step-level + step.config-nested). Path-reference
  configs are honored under v2 (G7 Step 4 rewriting).

**Recommended pattern for new workflows that gate:**

```yaml
# At workflow YAML top level:
config_version: 2          # default, can be omitted
gate_semantics: gate_to_bottom

links:
  cond_link:
    class: nanobrain.core.link.ConditionalLink
    source: a.x
    target: b.x
    condition: { op: exists, field: payload.value }
    # gate_semantics: gate_to_bottom    ← injected automatically by workflow-level field
    # auto_transfer: true               ← injected automatically under v2

steps:
  fan_in_step:
    class: my.MyFanInStep
    triggers:
      - class: nanobrain.core.trigger.AllDataReceivedTrigger
        # gate_semantics: gate_to_bottom    ← injected automatically
```

### Cycle-bearing workflow silent-failures (G99) — CURED 2026-05-17

Cycle-bearing workflows with `ConditionalLink` branching surfaced
four real silent-failure shapes during the G99 TDR-as-YAML
end-to-end testing. ALL FOUR loaded cleanly via `from_config` +
passed structural smoke tests; only `Workflow.run()` against real
Ollama exposed them. Pattern: "load passes, runtime appears to
work, no data ever propagates."

1. **`WorkflowGraph._get_cycles_info` was missing.** Referenced by
   `validate_graph` on the G18-Step-2 cycle-allowed success branch.
   Result: `AttributeError` swallowed by `handle_error`, validation
   reported as `(False, ['Graph validation failed'])`. Every
   cycle-bearing workflow with LoopController bounding refused to
   run. **Fixed** — method added.

2. **`ConditionalLink._init_from_config` didn't set
   `self.auto_transfer`.** `extract_component_config` pulled the
   value; `_init` never assigned it to `self`. The base class
   `_setup_automatic_transfer_if_possible` reads
   `getattr(self, 'auto_transfer', False)` — silent no-op for every
   ConditionalLink. **Fixed** — `self.auto_transfer = component_config.get('auto_transfer', True)`.

3. **`ConditionalLink.transfer` condition-met branch lacked
   data-unit `set()` fallback.** Only handled targets with
   `input_data_units` (list) or `set_input` method. Workflow-level
   outputs and step input data units accessed by name don't have
   either — they have `.set()`. The condition matched, the branch
   ran, no data transferred. The `gate_to_bottom` branch already had
   the fallback; this is parity. **Fixed** — added
   `await self.target.set(data)` fallback.

4. **`LoopController.process` didn't unwrap trigger envelope.**
   Framework delivers `{<input_du_name>: <payload>}` to Step's
   process; `CodeWriteStep` + `IsolatedPyExecStep` explicitly
   unwrap; `LoopController` didn't. Result: controller passed the
   wrapper through under `payload`, downstream back-edge consumers
   received doubly-wrapped data. **Fixed** — conservative single-key
   match against `step_input_data_units`.

**Detection signal for any future Step subclass**: if a Step
consumes payload-keyed input (i.e., its `process()` reads fields off
`input_data`), it MUST explicitly unwrap `{<input_du_name>: <payload>}`
shape at the top of `process()` — the framework wraps automatically
in data-driven mode. Pattern:

```python
async def process(self, input_data: dict, **kwargs):
    if (
        "<expected_input_du_name>" in input_data
        and isinstance(input_data["<expected_input_du_name>"], dict)
        and "<inner_key_that_distinguishes>" not in input_data
    ):
        input_data = input_data["<expected_input_du_name>"]
```

**Detection signal for any future Link subclass**: if the link's
`transfer()` branches on target type (looking for `input_data_units`
list / `set_input` method / etc), it MUST have the final
`await self.target.set(data)` fallback for bare data-unit targets.
Mirror what `gate_to_bottom` does today.

**Regression tests**: `nanobrain/tests/unit/test_g99_framework_fixes_2026_05_17.py` (15 tests).

### Reference cycle-bearing workflow

See `apecx_integration/composition/workflows/tdr_loop/tdr_refine_workflow.yml`
for the canonical shape of a cycle-bearing workflow:
* `allow_cycles: false` (cycle ALLOWED via G18 Step 2, not the
  workspace-wide hammer)
* DirectLink for initial input
* Two ConditionalLinks branching on the iteration step's output
* LoopController with `max_iterations: 3`
* Two ConditionalLinks on the controller output (continue / exhausted)
* The cycle: `iter_step → loop_gate → iter_step` (back-edge)

## Why this skill exists

The event-driven contract is the heart of nanobrain. Every "step never runs"
or "data didn't propagate" bug traces back to a missing trigger, a wrong link
target, or a misuse of a data unit subclass. This skill catalogs all three
abstractions and the wiring rules.

## File:line ground truth

| Concern | File | Approx. line |
|---|---|---|
| `DataUnitBase`, `DataUnitConfig` | `nanobrain/nanobrain/core/data_unit.py` | 31–69 |
| `DataUnitMemory` | `nanobrain/nanobrain/core/data_unit.py` | 1287 |
| `DataUnitFile` | `nanobrain/nanobrain/core/data_unit.py` | 1427 |
| `DataUnitString` | `nanobrain/nanobrain/core/data_unit.py` | 1609 |
| `DataUnitStream` | `nanobrain/nanobrain/core/data_unit.py` | 1742 |
| `DataUnit` (object-ref) | `nanobrain/nanobrain/core/data_unit.py` | 1897 |
| `_notify_change_listeners` | `nanobrain/nanobrain/core/data_unit.py` | 938–1029 |
| `_create_automatic_input_trigger` | `nanobrain/nanobrain/core/data_unit.py` | 749–803 |
| `TriggerBase`, `TriggerConfig`, `TriggerType` | `nanobrain/nanobrain/core/trigger.py` | 182–207 |
| `AsyncTriggerExecutor` | `nanobrain/nanobrain/core/trigger.py` | 29–180 |
| `DataUnitChangeTrigger` | `nanobrain/nanobrain/core/trigger.py` | 821–1123 |
| `AllDataReceivedTrigger` | `nanobrain/nanobrain/core/trigger.py` | 1129–1223 |
| `TimerTrigger` | `nanobrain/nanobrain/core/trigger.py` | 1225–1362 |
| `ManualTrigger` | `nanobrain/nanobrain/core/trigger.py` | 1364–1492 |
| `LinkBase`, `LinkConfig`, `LinkType` | `nanobrain/nanobrain/core/link.py` | 106–141 |
| `DirectLink` | `nanobrain/nanobrain/core/link.py` | 949–1303 |
| Self-reference rejection | `nanobrain/nanobrain/core/link.py` | 564–577 |
| Reference-format validation | `nanobrain/nanobrain/core/link.py` | 823–839 |

## Data unit taxonomy

All concrete DataUnit subclasses inherit from `DataUnitBase` and are created
via `from_config`. Direct construction raises `RuntimeError`. The five concrete
classes:

| Class | Purpose | Persistent? | Notes |
|---|---|---|---|
| `DataUnitMemory` | Generic in-memory value | No (default) | Most common; use unless you have a specific need |
| `DataUnitFile` | Backed by a file on disk | Yes (when configured) | Survives process restart; reads/writes JSON or text |
| `DataUnitString` | Text container with `append()` | No | Optimized for accumulating text |
| `DataUnitStream` | Async queue for streaming chunks | No | For LLM streaming, log streams, etc. |
| `DataUnit` | Holds a reference to an opaque Python object | No | When the value isn't serializable; ephemeral only |

### DataUnitConfig fields

```yaml
input_data_units:
  raw_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"   # required
    name: raw_input                                     # required
    description: "Raw input"                            # optional
    persistent: false                                   # File units only
    cache_size: 1000                                    # Stream units only
    file_path: "data/input.json"                        # File units only
    encoding: "utf-8"                                   # File/String units
    initial_value: ""                                   # String units
```

### Core API

All data units expose:

```python
await data_unit.set(value)        # write; fires SET event to listeners
await data_unit.get()             # read; no event
await data_unit.write(value)      # alias for set with explicit notification
await data_unit.read()            # alias for get
await data_unit.clear()           # erase; fires CLEAR event
await data_unit.exists()          # has-data check
data_unit.register_change_listener(callback)  # subscribe
```

### Event taxonomy (`DataUnitEventType`)

```
SET, GET, WRITE, READ, APPEND, DELETE, CLEAR, INITIALIZE, CLEANUP, ALL
```

The SET event is the one triggers care about most.

### When a data unit fires events

`set()` → `_set_internal_data(data, operation)` → `_notify_change_listeners(change_event)`
runs every registered listener as an `asyncio.Task` via `AsyncTriggerExecutor`.
Listeners are awaited concurrently with `asyncio.gather(..., return_exceptions=True)`.

The change event payload:

```python
{
    'data_unit_name': str,
    'operation': str,         # the DataUnitEventType value
    'old_data': Any,
    'new_data': Any,
    'timestamp': float,
    'operation_count': int,
}
```

## Trigger taxonomy

| Trigger | When it fires | Config fields |
|---|---|---|
| `DataUnitChangeTrigger` | A specified data unit changes | `data_unit: <name>`, optional `event_type` (default SET) |
| `AllDataReceivedTrigger` | All listed data units have data | `data_units: [name, name, ...]` |
| `TimerTrigger` | At interval | `timer_interval_ms: <int>` |
| `ManualTrigger` | Only when you call `await trigger.fire()` | (none) |

Common config (`TriggerConfig`):

```yaml
trigger_type: data_updated           # enum value (data_updated, all_data_received, timer, manual)
debounce_ms: 100                     # min ms between fires (default 100)
max_frequency_hz: 10.0               # rate cap (default 10.0)
condition: "<optional expression>"
timer_interval_ms: <int>             # for TimerTrigger
name: my_trigger
```

### How triggers watch (subscribe vs. poll)

- **DataUnitChangeTrigger** subscribes via `register_change_listener` on the
  watched data unit. Zero polling overhead. Fires immediately on SET.
- **AllDataReceivedTrigger** is **event-driven** (G120) — it registers a
  change-listener per input DU and re-checks on each change; the 100 ms poll is
  only a fallback for non-event-capable DU doubles. It is **RE-ARMABLE via value
  comparison** (2026-06-13): it fires the first time all inputs are present, then
  re-fires whenever the input value-tuple DIFFERS from the last fire — **"any input
  changed", NOT "all changed"**. A same-query re-run that only adds an approval to
  a *control* input (evidence input unchanged) still re-fires; a byte-identical
  re-run does NOT (the prior output is already correct). This makes a **cached,
  re-runnable** fan-in workflow correct — before the fix it fired once then went
  inert, so run 2+ returned STALE run-1 output. **Do NOT clear a fan-in's inputs to
  force a re-fire** (an earlier design did this and broke same-query re-runs: an
  upstream link suppresses no-op transfers, so a cleared-but-unchanged input is
  never re-delivered and the gate hangs at None). The fire-lock is created per
  running loop (a cached workflow runs across many event loops). Use for true
  multi-input gating; one input may arrive long before the other (early-arrival
  is handled).
- **TimerTrigger** uses `asyncio.sleep(interval)` in a loop; doesn't watch
  data units.
- **ManualTrigger** does nothing until you call `await trigger.fire()`.

### AsyncTriggerExecutor — deadlock prevention

Every trigger's callbacks are dispatched as background `asyncio.Task`s, never
awaited inline by the data unit's `set()` call. This prevents deadlocks when:

- A step's `process()` writes to its output data unit.
- That output unit fires a SET event.
- A trigger callback would normally await downstream step execution.
- Without the executor, the writer would deadlock on its own callback chain.

The executor also tracks `execution_stack` (set of currently-executing
trigger IDs) and refuses to recursively fire the same trigger:
`Preventing circular execution...`. And it rate-limits via
`execution_history` with a 100ms threshold to avoid runaway re-triggering.

## Link taxonomy

| Link | Use case | Notes |
|---|---|---|
| `DirectLink` | In-memory copy from source unit to target unit | Most common |
| `QueueLink` | Buffered queue between source and target | For backpressure |
| `TransformLink` | Apply a transformation function during transfer | `transform_function: "module.func"` |
| `ConditionalLink` | Transfer only if condition holds | `condition: "..."` |
| `FileLink` | Watch a file for changes; transfer file content | `file_path: ...` |
| `AcademyLink` | Distributed transfer via Academy/ProxyStore | HPC; uses `academy_agent_handle`, `proxystore_*` |

### LinkConfig fields

```yaml
class: "nanobrain.core.link.DirectLink"
config:
  source: "step_a.output"          # required, dot notation
  target: "step_b.input"           # required, dot notation
  link_type: direct                # matches the class
  buffer_size: 100                 # for QueueLink
  transform_function: "module.func"# for TransformLink
  condition: "x > 0"               # for ConditionalLink
  auto_transfer: true              # **REQUIRED for the link to actually fire.**
                                   # Default is FALSE — without this flag, the link silently no-ops:
                                   # workflow loads cleanly, trigger fires, but data NEVER transfers.
                                   # This is the dominant silent-failure shape in the codebase
                                   # (architecture.md §13 brutal-truth #3). Gap G7 proposes flipping
                                   # the default in workflow config schema v2.
  # Academy-specific:
  academy_agent_handle: my_agent
  action_name: process
  timeout_seconds: 30
  retry_attempts: 3
  proxystore_enabled: true
  proxystore_store_dir: /lus/flare/proxystore
  proxystore_store_name: my-workflow
  proxystore_connector_type: file
```

### Source/target dot notation

```
"workflow_input_unit"                # workflow-level data unit
"step_id.data_unit_name"             # step-owned data unit
```

If you write anything else, the framework raises:
```
ValueError: Invalid reference: <ref>. Must use 'step.data_unit' format
```

### Link transformation (data_mapping)

`LinkBase._transform_data` supports a `data_mapping` dict:

```yaml
data_mapping:
  output_field_name: source_dict_key_name
```

Useful when source emits a dict and target expects a renamed structure.

## The full event-driven trace

```
Step A: process() returns {'output': result}
  └─ framework writes result into A.output_data_unit via set()
       └─ A.output fires SET event (data_unit.py: 1404 for Memory)
            └─ _notify_change_listeners (data_unit.py: 938)
                 └─ AsyncTriggerExecutor schedules listener tasks
                      └─ DirectLink._on_source_data_changed callback fires
                           └─ link.transfer() calls B.input.set(transformed_data)
                                └─ B.input fires SET event
                                     └─ _create_automatic_input_trigger callback fires
                                          └─ Step B's executor schedules B.execute()
                                               └─ B._execute_process → B.process(input_data)
                                                    └─ ... and so on
```

Every transition is async, non-blocking, and tracked by the
`AsyncTriggerExecutor`. If something goes wrong, look at the executor's logs
for "Preventing circular execution" or rate-limit messages.

## Verbatim error messages

```
ValueError: Invalid reference: {ref}. Must use 'step.data_unit' format
```
> Fix the dot notation in your link's source/target.

```
ValueError: Invalid source reference: {source_ref}
ValueError: Invalid target reference: {target_ref}
```
> Same family — referenced step or data unit doesn't exist.

```
❌ ILLEGAL SELF-REFERENCING LINK: {source_name} to itself
```
> Source and target are the same data unit. Always a bug.

```
ValueError: Data unit class must be specified
ValueError: Data unit class must be from nanobrain.core.data_unit module
```
> Add `class:` to the data unit YAML; ensure it's a real DataUnit subclass.

```
ValueError: Unsupported config type: {type(config)}
```
> Component received a config of an unsupported type (not a path, dict, or
> Pydantic model). Likely you passed the wrong object.

```
ValueError: ConditionalLink requires condition configuration
```
> Add a `condition:` field to the link config.

## Pitfalls

1. **No trigger on the input data unit → step never runs.** The single most
   common bug. Always add a `DataUnitChangeTrigger` on each step input.
2. **Wrong dot notation.** `"step:input"` (colon) instead of `"step.input"`
   (dot) — invalid reference error.
3. **Self-referencing link.** Source and target the same data unit.
4. **Multiple triggers on the same input racing.** Use `debounce_ms` and
   `max_frequency_hz` to throttle. Default `debounce_ms: 100` is usually safe.
5. **Persistent data unit leak.** A `DataUnitFile` with `persistent: true`
   retains data across runs. Either `await unit.clear()` at workflow start
   or use `DataUnitMemory`.
6. **Stream backpressure.** `DataUnitStream` queues with `cache_size`; if
   producers outpace consumers, you OOM or block. Size the cache.
7. **Returning a key from `process` that doesn't match `output_data_units`.**
   The output unit doesn't update; the link doesn't fire.
8. **Sharing a data unit instance across two steps.** The framework relies
   on per-step ownership of the change-listener list. Sharing creates
   nondeterministic behavior. Each step instantiates its own.

## Pattern: a complete two-step wiring

```yaml
# config/wiring_demo.yml — workflow
name: wiring_demo
input_data_units:
  raw:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: raw

output_data_units:
  result:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: result

steps:
  prep:
    class: my_project.steps.PrepStep
    config: config/prep_step.yml
  agg:
    class: my_project.steps.AggStep
    config: config/agg_step.yml

links:
  raw_to_prep:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: raw
      target: "prep.input"
      link_type: direct

  prep_to_agg:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: "prep.output"
      target: "agg.input"
      link_type: direct

  agg_to_result:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: "agg.output"
      target: result
      link_type: direct
```

```yaml
# config/prep_step.yml
name: prep
input_data_units:
  input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: input
output_data_units:
  output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: output
triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: input
```

`agg_step.yml` is symmetric.

To run the workflow: `await workflow.input_data_units['raw'].set(my_data)`,
then await `workflow.output_data_units['result'].get()` (with a timeout).

## Checklist

- [ ] Every step input has at least one trigger registered.
- [ ] All link source/target references use dot notation.
- [ ] No self-referencing links.
- [ ] `persistent: true` only on `DataUnitFile`, and only if you actually want cross-run state.
- [ ] If you used a cycle, you set `allow_cycles: true` AND configured `debounce_ms` on the trigger.
- [ ] No data unit instance is shared across two steps (each step owns its own).
- [ ] Smoke test verifies the wiring loads; integration test against real data is recorded.

## Recent silent-failure closures (2026-05-09)

### G44 — DataUnitProxyRef unscoped namespace warning

Pre-G44 ``DataUnitProxyRef.namespace()`` silently returned ``""`` when no
``WorkflowRunContext`` was active, leaving the proxystore key under a
GLOBAL identity instead of per-run isolation. Multi-tenant runs
sharing a Redis-backed ProxyStore could collide on equality / hashing
without any visible signal — same shape as G7's
``auto_transfer=False`` silent failure.

Post-G44:
- Default mode: emits a one-time ``WARNING`` per ``DataUnitProxyRef``
  instance, naming the data unit + reason (no run context vs.
  library import failure).
- ``NANOBRAIN_STRICT_NAMESPACE=1`` flips the WARNING into a
  ``ComponentConfigurationError`` so deployments where multi-tenant
  isolation MUST hold (HPC bundles, shared-Redis ProxyStore) FAIL-FAST.

You don't need to do anything new — the framework catches you. To
suppress the warning, EITHER set
``DataUnitProxyRef.proxystore_namespace_prefix`` explicitly OR run
inside a ``WorkflowRunContext.activate()``.

### G39 — config_version: 2 lint enforcement

The lint script
``apecx-mcp-integration/scripts/lint_workflow_yamls.py`` (wired as a
pre-commit hook) now fails CI when:

  R1. A workflow YAML (file under canonical roots WITH a ``links:``
      block) lacks ``config_version: 2``.
  R2. An inline ``DirectLink`` config block omits ``auto_transfer: true``.
  R3. A path-reference ``DirectLink`` (``config: "<path>.yml"``) points
      at a YAML that lacks ``auto_transfer: true``.

Both ``config_version: 2`` AND explicit ``auto_transfer: true``
on every DirectLink are now required. Even though G7 Step 5 made
True the field default, the explicit declaration is the
lint-detectable signal that protects against regressions.
