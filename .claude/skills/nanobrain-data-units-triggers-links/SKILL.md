---
name: nanobrain-data-units-triggers-links
description: The event-driven contract — how DataUnits emit change events, how Triggers subscribe (or poll), and how Links transfer data from one step's output to another step's input. Read whenever you wire steps together, define triggers, or add a link. Covers all DataUnit subclasses (Memory/File/String/Stream/object-ref), all Trigger types (DataUnitChange/AllDataReceived/Timer/Manual), all Link types (Direct/Queue/Transform/Conditional/File/Academy), the AsyncTriggerExecutor's deadlock prevention, and the verbatim error messages.
---

# nanobrain-data-units-triggers-links

## ⚠️ DOMINANT SILENT-FAILURE WARNING

**Every `DirectLink` (and `TransformLink` / `ConditionalLink` / `FileLink`)
MUST declare `auto_transfer: true` in its YAML config.** The framework
default is `False`. Without the flag the link silently no-ops:

- `Workflow.from_config()` succeeds.
- The trigger cascade fires.
- `process()` runs on every step.
- **No data ever transfers.** Downstream consumers see empty inputs.
- No exception. No log message at WARNING level. The workflow appears to "work".

This is the dominant bug in the codebase — `architecture.md §13`
brutal-truth #3. Gap **G7** (`nanobrain_capability_gaps.md`) proposes
flipping the default in `config_version: 2`. Until G7 ships, every
hand-authored link must declare it explicitly.

**Detection signal:** the workflow completes "successfully" but downstream
data units hold their initial value (often `None` or empty list/dict).
Run `Workflow.wait_for_cascade()` synchronously after `process()`; if no
state change at downstream units, this is your bug.

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
- **AllDataReceivedTrigger** *polls* every 100 ms until all listed units have
  non-null data, then fires once. Higher overhead; use only for true
  multi-input gating.
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
