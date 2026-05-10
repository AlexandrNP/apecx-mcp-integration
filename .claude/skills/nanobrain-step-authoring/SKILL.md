---
name: nanobrain-step-authoring
description: How to author a `BaseStep` (or `Step`) subclass correctly. Covers the execute/process/_execute_process triad (you implement `process`, never override `execute`), the data-unit and trigger ownership boundary, the StepConfig schema, the FAIL-FAST validations the framework runs at step initialization, and the verbatim error messages that signal each violation. Read this whenever you create a new step class or modify an existing one.
---

# nanobrain-step-authoring

## Why this skill exists

Steps are the fundamental processing unit in nanobrain. Authoring one wrong
will produce `FAIL-FAST: ...` errors at initialization or — worse — a step that
silently never runs because no trigger watches its input. This skill shows the
exact contract.

> **Output-key silent-failure.** A step's `process()` returns a dict; the
> framework writes each value to the output data unit named by the matching
> key. **If your dict's key does not match a declared `output_data_units`
> name, the value is silently dropped** (no exception, no log warning).
> Downstream consumers see the data unit's initial value. Always cross-check:
> the keys you `return` MUST equal the data unit names you declared in the
> step's YAML `output_data_units:` block. **Gap G6 SHIPPED 2026-05-09** —
> declare `step_output_schema:` (with G6's SchemaRef) on StepConfig and the
> framework FAIL-FASTs at runtime on schema mismatch.

> **Validation/repair as Steps, not custom code.** Per
> `nanobrain_alignment_audit.md` F-2/F-3/F-6: 5-gate validation pipelines and
> repair loops belong inside the workflow as `BaseStep` chains connected by
> `ConditionalLink`s, not as imperative Python sitting outside the workflow.

## Available framework primitives (shipped 2026-05-09)

When authoring a step that needs any of the following, the framework
ships the primitive — DO NOT roll your own:

| Need | Use | Where |
|---|---|---|
| Typed input/output validation at the wire boundary | `step_input_schema` / `step_output_schema` field on StepConfig | G6 — `nanobrain.core.step.SchemaRef` |
| Resource projection for HPC sizing + cost gating | `resource_envelope` field on StepConfig | G12 — `nanobrain.core.step.ResourceEnvelope` |
| Bounded loops (declared back-edges) | Use `LoopController` step + `ConditionalLink` predicate | G18 — `nanobrain.library.steps.LoopController` |
| Tool dispatch (Rhea / Galaxy / native) | Subclass or instantiate `ToolExecutionStep` | G11 — `nanobrain.library.steps.ToolExecutionStep` |
| Tool descriptor (input/output schema, cost, capability) | Inline dict OR `tool_descriptor_path:` in StepConfig | G15 — `nanobrain.core.tool.UnifiedToolDescriptor` |
| Per-step provenance recording | Activate a `ProvenanceContext` with sink_path | G4 — `nanobrain.core.provenance.ProvenanceContext` |

## File:line ground truth

| Concern | File | Approx. line |
|---|---|---|
| `BaseStep` class definition | `nanobrain/nanobrain/core/step.py` | ~250 |
| `StepConfig` schema | `nanobrain/nanobrain/core/step.py` | 35–82 |
| `execute()` method | `nanobrain/nanobrain/core/step.py` | 1358–1410 |
| `_execute_process()` bridge | `nanobrain/nanobrain/core/step.py` | 1478–1480 |
| `process()` abstract | `nanobrain/nanobrain/core/step.py` | 1670–1671 |
| `_validate_step_implementation` (FAIL-FAST) | `nanobrain/nanobrain/core/step.py` | 641–668 |
| `_update_output_data_units` | `nanobrain/nanobrain/core/step.py` | 1482–1520 |

## The execute/process/_execute_process triad

```
external caller
      │
      ▼
async execute(**kwargs)              ← infrastructure: collects input from
      │                                input_data_units, applies timeout,
      │                                delegates to executor; DO NOT override
      ▼
async _execute_process(input_data)   ← internal bridge; DO NOT override
      │
      ▼
async process(input_data, **kwargs)  ← business logic; YOU implement this
      │
      ▼
returned dict / value
      │
      ▼
_update_output_data_units(result)    ← infrastructure: writes to output
                                       data units, fires trigger events
```

You write **only `process`**. The framework owns the rest.

## The mandatory `process` signature

```python
from nanobrain.core.step import BaseStep

class MyStep(BaseStep):
    @classmethod
    def _get_config_class(cls):
        from nanobrain.core.step import StepConfig
        return StepConfig

    async def process(self, input_data: dict, **kwargs):
        # input_data is a dict keyed by your step's input data unit names.
        raw = input_data['raw_input']                 # data unit -> value
        result = self._do_work(raw)
        return {'prepared_data': result}              # keys must match output unit names
```

**Three rules the framework FAIL-FAST-validates at step init:**

1. The method name **must** be `process` (not `execute`, not `run`).
2. It **must** be `async def`. Sync `def process(...)` raises:
   ```
   FAIL-FAST: Step {name} ({class}).process() must be async.
   Change 'def process' to 'async def process'.
   ```
3. The class **must** define `process` itself; inheriting it without override is
   not enough for non-trivial steps. Missing entirely raises:
   ```
   FAIL-FAST: Step {name} ({class}) missing required 'process' method.
   Add 'async def process(self, input_data, **kwargs)' to your step class.
   ```

## What `process` returns and how outputs flow

`process` returns a dict (or scalar for single-output steps). For each key in
the returned dict that matches an `output_data_units` name, the framework
writes the value into that data unit, which fires a `SET` event, which any
downstream link's listener picks up.

```python
async def process(self, input_data, **kwargs):
    # output_data_units in YAML defined two outputs: 'prepared' and 'metadata'
    return {
        'prepared': processed_records,
        'metadata': {'count': len(processed_records), 'source': input_data['raw_input']},
    }
```

If you return a key the YAML didn't declare, the framework typically logs a
warning and ignores it. If you omit a declared key, that downstream data unit
never updates and downstream steps never fire. Be explicit.

## NEVER override `execute`

You will see `execute()` on `BaseStep`. Do not subclass-override it. The
framework's policy checker (`.claude/scripts/review_policy_check.py`) flags
this, and the framework itself rejects it at initialization with FAIL-FAST.

If you genuinely think you need to override `execute` (e.g., to integrate a
non-pickleable executor or short-circuit some setup), that's an
architecture-level discussion — bring it to the user before touching the
method.

## Step ownership boundary

A step **owns**:

- `input_data_units` (Dict[str, DataUnit]) — its inputs
- `output_data_units` (Dict[str, DataUnit]) — its outputs
- `triggers` (List[Trigger]) — what fires its execution
- `tools` (Dict[str, Tool]) — tools attached to this step (rare for plain steps)

A step **does NOT own**:

- Links to or from other steps (those belong to the workflow).
- Other steps' data units (you reference them through links, not directly).

The workflow owns:
- `child_steps: Dict[str, BaseStep]`
- `step_links: Dict[str, LinkBase]`
- workflow-level `input_data_units` / `output_data_units` (entry/exit points)

If you find yourself wanting a step to read another step's data unit directly,
you are skipping a link. Add the link in the workflow YAML instead.

## StepConfig schema

Required:

| Field | Type | Notes |
|---|---|---|
| `name` | str | Unique within the workflow |

Optional:

| Field | Type | Default | Notes |
|---|---|---|---|
| `description` | str | "" | Human-readable purpose |
| `auto_initialize` | bool | true | |
| `debug_mode` | bool | false | |
| `enable_logging` | bool | true | |
| `log_data_transfers` | bool | true | |
| `log_executions` | bool | true | |
| `input_data_units` | Dict[str, DataUnitConfig] | {} | Step-owned inputs |
| `output_data_units` | Dict[str, DataUnitConfig] | {} | Step-owned outputs |
| `triggers` | List[Dict \| TriggerBase] | [] | What fires execution |
| `executor_config` | ExecutorConfig | None | Step-specific executor; otherwise inherits from workflow |
| `tools` | Dict[str, Dict] | {} | Tools attached to this step |
| `execution_timeout` | float | 300 | Per-execution timeout (seconds) |

## Minimal step YAML

```yaml
# config/simple_step.yml
name: simple_step
description: "Reads raw input, returns processed value"

input_data_units:
  raw_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "raw_input"

output_data_units:
  processed:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "processed"

triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "raw_input"
```

```python
# my_project/steps/simple_step.py
from nanobrain.core.step import BaseStep, StepConfig

class SimpleStep(BaseStep):
    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data, **kwargs):
        return {'processed': input_data['raw_input'].upper()}
```

## Fully-featured step YAML (with executor and tools)

```yaml
name: code_generation_step
description: "Generates code from natural language input"
debug_mode: true
enable_logging: true

input_data_units:
  input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: input
    description: "Code generation request"

output_data_units:
  output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: output
    description: "Generated code"

triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "input"

executor:
  class: "nanobrain.core.executor.LocalExecutor"
  config:
    executor_type: "local"
    name: code_gen_executor
    max_workers: 2
    timeout: 60

execution_timeout: 120
```

## The trigger you almost always need

A step without a trigger on its input data unit **never runs**. The framework
does not implicitly create one. The most common trigger is:

```yaml
triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "<input data unit name>"
```

For multi-input steps, use `AllDataReceivedTrigger`:

```yaml
triggers:
  - class: "nanobrain.core.trigger.AllDataReceivedTrigger"
    data_units: ["input_a", "input_b"]
```

See `nanobrain-data-units-triggers-links/SKILL.md` for the full taxonomy.

## Async-only contract

- `process()` MUST be `async def`. Framework FAIL-FAST.
- Inside `process()`, all I/O should be awaited (`await db.fetch(...)`,
  `await client.call(...)`). Synchronous blocking I/O inside `process()` will
  freeze the event loop and starve other steps.
- For CPU-bound work, use `ProcessExecutor` or wrap in
  `await asyncio.to_thread(...)`. See `nanobrain-executors/SKILL.md`.

## Logging

Use `self.nb_logger`, not `self.logger`. The framework's `_validate_step_implementation`
checks the source for `self.logger` references and raises FAIL-FAST if found.
Why: `self.logger` is a name owned by some other base; using it would shadow
the framework's logger.

```python
async def process(self, input_data, **kwargs):
    self.nb_logger.info(f"processing {len(input_data)} inputs")
    ...
```

## Verbatim error messages

```
FAIL-FAST: Step {name} ({ClassName}) missing required 'process' method.
Add 'async def process(self, input_data, **kwargs)' to your step class.
```
> Add the method.

```
FAIL-FAST: Step {name} ({ClassName}).process() must be async.
Change 'def process' to 'async def process'.
```
> Add `async`.

```
FAIL-FAST: Step {name} ({ClassName}) uses 'self.logger' on lines [...].
Use 'self.nb_logger' instead for framework compliance.
```
> Search-and-replace `self.logger` → `self.nb_logger`.

```
ValueError: Step '{step_id}' not found in workflow
```
> A workflow link references a step name that doesn't exist. Check spelling
> and ensure the step is declared in the workflow's `steps:` block.

```
ComponentConfigurationError: Data unit configuration missing 'class' field
```
> Every nested data unit reference must have `class:`.

## Pitfalls

1. **No trigger → no execution.** Step accepts data into its input unit but
   `process()` never fires. Add a trigger.
2. **Returned dict keys don't match output_data_units names.** Downstream
   listeners never wake up. Print the dict you return and cross-check the YAML.
3. **Used `self.logger` instead of `self.nb_logger`.** FAIL-FAST.
4. **Sync `def process`.** FAIL-FAST.
5. **Overrode `execute`.** Policy checker flags it; framework rejects it.
6. **Read another step's data unit directly.** Wire a link instead.
7. **Two steps share the same data unit instance.** Ownership confusion;
   creates race conditions on the change-listener list. Each step owns its own
   data units; cross-step communication happens via links.

## Checklist

- [ ] Subclass `BaseStep` (or `Step`).
- [ ] Implemented `_get_config_class`.
- [ ] Implemented `async def process(self, input_data, **kwargs)`.
- [ ] Did not override `execute` or `_execute_process`.
- [ ] Used `self.nb_logger` for all logging.
- [ ] Declared `input_data_units`, `output_data_units`, and `triggers` in YAML.
- [ ] Returned dict from `process` whose keys exactly match `output_data_units` names.
- [ ] Smoke test exists; integration test against real (small) data is recorded.

## New step-level primitives (2026-05-09 — eval_03 Tier 1-3 chain)

### Provenance auto-recording (G4-completion)

Pre-G4-completion: step authors who wanted provenance had to wrap
every ``process()`` call by hand; exceptions silently disappeared.
Post-G4-completion: ``BaseStep._execute_process`` consults
``current_provenance_context()`` and records inputs / outputs /
exception / timing on every ``process()`` call automatically.

**Step authors do nothing.** Just write ``async def process(...)``;
when a ``ProvenanceContext`` is active the framework records each
invocation. Recorder errors are swallowed (observability never
masks the step's real exception or return value).

### Step-event publishing (G37)

Same wrap also publishes step-lifecycle events. Subscribers receive
``step_start`` / ``step_complete`` / ``step_failed`` synchronously.
Step authors do nothing — the events fire from
``_execute_process``. See ``nanobrain/core/step_events.py``.

### Suspend semantics (G27 — DeferredHITLStep)

For "the step needs to wait for human approval, possibly minutes or
hours, possibly across process restarts":

```python
from nanobrain.library.steps.deferred_hitl_step import DeferredHITLStep

# In your workflow YAML:
# steps:
#   approval_gate:
#     class: nanobrain.library.steps.deferred_hitl_step.DeferredHITLStep
#     config:
#       name: approval_gate
#       prompt_template: "Approve {input.action} on {input.target}?"
```

Construction: ``DeferredHITLStep.from_config(path, approval_store=...)``.
Storage backends ship: ``InMemoryApprovalStore`` (tests),
``FileApprovalStore`` (production single-process; survives restart).

The step is idempotent + stateless: first call submits an Approval
and raises ``ApprovalPendingError``; retry finds the existing record
via deterministic ``approval_id`` and re-raises (or returns the
resolved decision payload). ``rejected`` decision raises
``ApprovalRejectedError`` (workflow-terminal).

### Capability-required tools (G28)

If your step dispatches a tool whose UTD declares
``requires_capability: [...]``, the framework now enforces that
list against the active ``WorkflowRunContext.capability_tokens``
BEFORE invoking the adapter. ``CapabilityNotGranted`` raises;
your ``process()`` does not need to check by hand.

### Cost recording (G26)

Cost-emitting steps (LLM clients, expensive tool calls) call
``record_cost(kind, amount)`` to charge against the active tracker:

```python
from nanobrain.core.cost_envelope import record_cost

async def process(self, input_data, **kwargs):
    response = await self._llm.complete(prompt)
    record_cost("tokens", response.usage.total_tokens)
    record_cost("usd", response.usage.cost_usd)
    return {"answer": response.content}
```

When no tracker is active, ``record_cost`` is a fast no-op.
``CostEnvelopeBreach`` raises when a record would exceed the cap.
