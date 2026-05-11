---
name: nanobrain-workflow-authoring
description: How to author a Workflow YAML and (rarely) a Workflow subclass. Workflows are Steps that orchestrate other Steps via links. Covers the WorkflowConfig schema, step registration, link wiring, executor selection per step, DAG validation rules (cycle detection, orphan detection, self-referencing-link prohibition), execution model (event-driven vs. imperative), and the verbatim error messages from `workflow_graph.py`.
---

# nanobrain-workflow-authoring

## ⚠️ Read first — silent-failure shapes

The framework's static validators catch structural errors (cycles, orphans,
unknown step IDs). They do **not** catch the four dominant runtime
silent-failure shapes that make a workflow appear to work while producing
nothing useful. Cross-reference `architecture.md §13` for the brutal-truth
list. Summary:

1. **Every `DirectLink` in your YAML must declare `auto_transfer: true`.**
   Default is False. Without the flag, the link silently no-ops on every
   trigger. (Gap **G7** proposes flipping the default in `config_version: 2`.)
2. **`AllDataReceivedTrigger`'s expected set is bound at trigger init.** A
   gated-off layer that never publishes deadlocks the trigger. (Gap **G2**
   proposes a dynamic `expected_set_source`; gap **G10** proposes
   gate-to-bottom semantics.)
3. **Workflow-level data unit shape mismatch silently drops payloads.** Both
   `input_data_units` and `output_data_units` must be declared at workflow
   level AND the link's `target` must match a declared workflow output by
   exact name.
4. **Trigger payload wrapping (`{unit_name: payload}` envelope) is the
   framework's responsibility.** A step's `process()` sees the **raw payload**;
   if you wrap it again you ship a doubly-wrapped value downstream.

After every workflow change, call `Workflow.wait_for_cascade(timeout, settle_ms)`
synchronously after `process()` and assert the workflow-level outputs
actually carry your expected payload — don't trust trigger-cascade
"completion" without checking the data.

### Pre-execution validation across all three authoring paths (2026-05-11)

CLAUDE.md describes three legit workflow-authoring paths. All three
now route through the same framework-rule validator before runtime:

| Path | Validator entry point | Surfaces violations as |
|---|---|---|
| Hand-authored YAML | `Workflow.from_config(path)` | Framework `ValueError` / `ComponentConfigurationError` |
| LLM composer | `Composer.compose(prompt)` | Structured `WorkflowValidationError` with per-rule `WorkflowViolation` records; retries once with feedback payload (C1) |
| Lightweight `WorkflowBuilder` | `validate_and_load(builder)` from `apecx_integration.composition.lightweight_validator` | Structured `WorkflowValidationError` (same payload as composer) |

The structured surface (`rule_id`, `path`, `message`,
`suggested_fix`) is the same across paths so retry / repair logic
can be reused.

## Alignment-audit findings — author validation/repair as Steps, not pseudocode

Per `nanobrain_alignment_audit.md` F-2, F-3, F-6: the 5-gate validation
pipeline and the repair loop are **nanobrain Steps connected by Links**, not
imperative Python. If you find yourself writing a custom Python "validator
runner" or "repair loop" outside the workflow YAML, stop — it's a duplicate
of `LoopController` (gap **G18**) and `BaseStep` (existing). Cross-reference
`agent_workflow_authoring.md §6` for the canonical 5-gate-as-Steps shape.

## Why this skill exists

A workflow is the wiring diagram for a multi-step computation. Get it wrong
and you'll get one of: a step that never fires, a deadlock from a self-loop,
an `Orphaned steps found (no connections)` validation error, or a workflow that
"runs" but produces no output because no link connects to the workflow's
declared outputs (or every link silently no-ops; see warning above).

## File:line ground truth

| Concern | File | Approx. line |
|---|---|---|
| `Workflow` (subclasses `Step`) | `nanobrain/nanobrain/core/workflow.py` | ~466 |
| `WorkflowConfig` schema | `nanobrain/nanobrain/core/workflow.py` | 38–96 |
| `_init_from_config` for Workflow | `nanobrain/nanobrain/core/workflow.py` | 1554–1632 |
| Step + link registration | `nanobrain/nanobrain/core/workflow.py` | 1131–1140 |
| Step lookup error | `nanobrain/nanobrain/core/workflow.py` | 1281–1282 |
| `WorkflowGraph` (DAG) | `nanobrain/nanobrain/core/workflow_graph.py` | full file |
| `validate_graph` | `nanobrain/nanobrain/core/workflow_graph.py` | 286–374 |
| `has_cycles` (DFS) | `nanobrain/nanobrain/core/workflow_graph.py` | 186–213 |

## Mental model

A workflow is a **Step** (literally inherits from `Step`). It has its own
`input_data_units` and `output_data_units` — these are the workflow's
entry/exit ports. Inside, it owns:

- `child_steps: Dict[str, BaseStep]` — the steps it orchestrates
- `step_links: Dict[str, LinkBase]` — the links wiring data between steps
- a `WorkflowGraph` — a DAG of step IDs and edges (links)

When the workflow runs, **data flows event-driven**: deposit something into
the workflow's input data unit → the link from that input fires → the first
step's input unit receives data → the first step's trigger fires → it
executes → its output unit fires → the next link fires → and so on, until the
workflow's output unit gets written.

## Minimal workflow YAML

```yaml
# config/two_step_workflow.yml
name: two_step_workflow
description: "Reads input, processes, returns result"

input_data_units:
  raw_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: raw_input

output_data_units:
  final_result:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: final_result

steps:
  prep:
    class: my_project.steps.PreparationStep
    config: config/prep_step.yml

  aggregate:
    class: my_project.steps.AggregationStep
    config: config/aggregate_step.yml

links:
  input_to_prep:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: "raw_input"          # workflow-level data unit
      target: "prep.input"         # step's input unit
      link_type: direct

  prep_to_aggregate:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: "prep.output"
      target: "aggregate.input"
      link_type: direct

  aggregate_to_output:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: "aggregate.output"
      target: "final_result"       # workflow-level data unit
      link_type: direct
```

## Fully featured (mixed-execution) workflow

This pattern is the canonical reference; it uses workflow-level data units,
multiple executors, and Academy links for HPC delegation.

```yaml
# config/mixed_execution_workflow_aurora.yml
name: academylink_aurora_workflow
version: "2.0"

input_data_units:
  raw_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: raw_input
    persistent: false

output_data_units:
  final_results:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: final_results
    persistent: false

executors:
  local_executor:
    executor_type: local
    name: local_executor
    max_workers: 2
    timeout: 60
  aurora_executor:
    executor_type: parsl
    name: aurora_executor
    parsl_config_file: ../aurora_parsl_executor.yml
    timeout: 600

steps:
  data_preparation:
    class: demos.academylink_aurora_demo.steps.DataPreparationStep
    config: config/data_preparation_step.yml
    executor: local_executor
  aurora_computation:
    class: demos.academylink_aurora_demo.steps.AuroraComputationStep
    config: config/aurora_computation_step.yml
    executor: aurora_executor
  result_aggregation:
    class: demos.academylink_aurora_demo.steps.ResultAggregationStep
    config: config/result_aggregation_step.yml
    executor: local_executor

links:
  aurora_computation_link:
    class: "nanobrain.academy_integration.academy_link.AcademyLink"
    config: "config/aurora_computation_link.yml"
  aurora_results_link:
    class: "nanobrain.academy_integration.academy_link.AcademyLink"
    config: "config/aurora_results_link.yml"

execution:
  timeout: 600
  retry_attempts: 2
  parallel_execution: false
```

Source: `nanobrain/demos/academylink_aurora_demo/`. Read those files when you
need a working reference.

## WorkflowConfig schema

Inherits StepConfig fields, adds:

| Field | Type | Default | Notes |
|---|---|---|---|
| `enable_monitoring` | bool | true | Enables resource monitor |
| `max_parallel_steps` | int | 10 | Concurrency cap |
| `step_timeout` | float | 300 | Per-step default timeout |
| `retry_attempts` | int | 3 | On step failure |
| `retry_delay` | float | 1.0 | Seconds between retries |
| `allow_cycles` | bool | false | If true, validation tolerates cycles (rare) |
| `validate_graph` | bool | true | Run DAG validation at init |
| `require_connected_graph` | bool | true | Disconnected components → error |
| `steps` | Dict[str, StepRef] | {} | Map of step_id → class+config |
| `links` | Dict[str, LinkRef] | {} | Map of link_id → class+config |
| `executors` | Dict[str, ExecutorConfig] | {} | Named executors usable by steps |

A `StepRef` is `{class: "...", config: "...", executor: "name"}`. A `LinkRef`
is `{class: "...", config: "..."}`. The `executor` field on a step ref
references one of the named entries in the workflow's `executors` map.

## How executors are resolved per step

```yaml
steps:
  heavy_step:
    class: my_project.steps.HeavyStep
    config: config/heavy_step.yml
    executor: aurora_executor   # name from workflow's `executors:` map
```

The workflow's `_init_from_config` builds each named executor from its config,
then injects it into each step's dependencies during the step's own
`from_config` call. If a step doesn't specify `executor:`, it inherits the
workflow's executor.

## Link wiring and dot notation

Source/target strings use dot notation: `step_id.data_unit_name` or just
`workflow_data_unit_name` for workflow-level units.

```yaml
links:
  prep_to_compute:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: "prep.output"          # step prep's output data unit
      target: "compute.input"        # step compute's input data unit
      link_type: direct
```

Invalid forms the framework rejects:

- `source: "prep"` → missing data unit; `Invalid reference: prep. Must use 'step.data_unit' format`.
- `source: "prep:output"` → wrong separator (colon instead of dot); same error.
- `source: "prep.output"`, `target: "prep.output"` → self-reference;
  `❌ ILLEGAL SELF-REFERENCING LINK: {link_id} connects step '{prep}' to itself.`

See the dedicated skill `nanobrain-data-units-triggers-links` for full link
type taxonomy.

## DAG validation rules

`WorkflowGraph.validate_graph` runs at workflow initialization (when
`validate_graph: true`, which is the default). It enforces:

1. **Non-empty graph.** `Workflow graph is empty - no steps defined`.
2. **No cycles** (unless `allow_cycles: true`).
   `⚠️ WORKFLOW CYCLES DETECTED: This workflow contains cycles. Ensure that appropriate resolution mechanisms are in place...`
3. **Connected** (unless `require_connected_graph: false`).
   `Workflow graph is not connected - contains isolated components`.
4. **No orphans** (steps with no inbound and no outbound edges, when there's
   more than one step).
   `Orphaned steps found (no connections): [...]`.
5. **No self-referencing links.**
   `❌ ILLEGAL SELF-REFERENCING LINK: {link_id} connects step '{source_id}' to itself.
   Self-referencing links are prohibited in the workflow architecture as they
   create infinite trigger loops and prevent proper workflow execution.`

If any rule is violated, the workflow raises an error during `from_config` and
never reaches a runnable state.

## Execution model

**Event-driven (default).** You don't tell the framework "run step A, then B".
You deposit data into the workflow's input data unit, and the trigger graph
takes over. Step A's trigger fires when its input arrives; A runs; A writes
output; the link to B fires; B's trigger fires; etc.

**Imperative (rare, opt-in via `divergence_enabled` etc.).** A topological
sort of the DAG is generated and the workflow walks the order itself. Use
only when event-driven flow is structurally impossible (e.g., a step that
fans out and rejoins).

For 99% of workflows: event-driven is correct. Set up your data units and
triggers properly and the framework runs the graph for you.

## Workflow vs. Step ownership

| Owns | Workflow | Step |
|---|---|---|
| Workflow-level entry/exit data units | ✅ | — |
| Step's `input_data_units`, `output_data_units` | — | ✅ |
| Step's `triggers` | — | ✅ |
| `child_steps` map | ✅ | — |
| `step_links` map (links between steps) | ✅ | — |
| Named executors usable across steps | ✅ | — |
| Per-step executor binding | ✅ (assigns) | ✅ (uses) |

If you find yourself wanting the workflow to manipulate a step's internal data
unit directly, you are violating the boundary. Add a link.

## Verbatim error messages

```
ValueError: Step '{step_id}' not found in workflow
```
> A link references a non-existent step. Check the `steps:` block names.

```
Workflow graph is empty - no steps defined
```
> Add at least one step.

```
Orphaned steps found (no connections): ['step_a', 'step_b']
```
> These steps have no inbound or outbound links. Either wire them up or remove them.

```
Workflow graph is not connected - contains isolated components
```
> Two disjoint subgraphs exist. Connect them with a link, or split into two workflows.

```
⚠️ WORKFLOW CYCLES DETECTED: This workflow contains cycles. Ensure that
appropriate resolution mechanisms are in place...
```
> A cycle was detected. Either set `allow_cycles: true` (and verify your
> debounce settings prevent infinite firing) or break the cycle.

```
❌ ILLEGAL SELF-REFERENCING LINK: {link_id} connects step '{source_id}' to itself.
Self-referencing links are prohibited in the workflow architecture as they create
infinite trigger loops and prevent proper workflow execution.
```
> Source and target are the same step's data unit. Almost always a typo or
> a wrong design — break the cycle.

## Pitfalls

1. **No link from workflow input to first step.** The workflow input unit
   gets data, but no listener fires. Add `input_to_first_step` link.
2. **No link from last step to workflow output.** The pipeline runs but the
   workflow's declared output never updates; downstream callers see nothing.
3. **Dot-notation typo.** `"step.outpu"` (missing `t`) — silent failure
   because the data unit name doesn't match anything; the link sits there
   waiting forever.
4. **Cycle without debounce.** Even with `allow_cycles: true`, an unthrottled
   cycle fires indefinitely. Use `debounce_ms` on the trigger to throttle.
5. **Step's `executor:` references a name not in `executors:`.** Step
   initialization fails because the dependency cannot be resolved.
6. **Mixed ownership mistake.** Putting a step's I/O units into the
   workflow-level `input_data_units` because "they're the workflow's input
   too" — this conflates entry-point ownership with internal step ownership.
   Keep them separate; wire with a link.

## Checklist

- [ ] Workflow YAML has both `input_data_units` and `output_data_units` at
      the top level (entry/exit ports).
- [ ] Every step is declared under `steps:` with `class:` and `config:`.
- [ ] Every step's input data unit is the target of at least one link
      (otherwise it never fires).
- [ ] Every workflow output data unit is the target of at least one link
      from a step (otherwise the workflow produces nothing).
- [ ] Every link uses dot notation `step.data_unit` or a workflow-level unit name.
- [ ] No self-referencing links.
- [ ] DAG passes `validate_graph` (cycles, orphans, connectedness).
- [ ] Executor names referenced by steps exist in `executors:` block.
- [ ] Smoke test loads the workflow without error; integration test against
      real data is recorded.

## New primitives (2026-05-09 — eval_03 Tier 1-3 chain)

Two ergonomic additions to ``Workflow``:

### ``Workflow.from_skeleton(skeleton, bindings)`` — G9-completion

A *parameterized* workflow loader. The skeleton declares typed holes;
the caller binds them; the framework lowers + loads. Use when the same
workflow shape runs against many parameter sets (or when an LLM agent
picks a skeleton from a registry).

```python
wf = Workflow.from_skeleton(
    "configs/skeletons/rag_pipeline.yml",
    bindings={"corpus": "pubmed_2025_q1", "min_evidence": 5},
)
```

Skeleton input may be a Path/str (YAML file), a Skeleton instance, or
an inline dict. Missing required holes FAIL-FAST; extra binding keys
not declared in skeleton.holes FAIL-FAST. Optional holes get their
declared default. See ``nanobrain/library/orchestration/skeleton.py``
for the Skeleton schema and ``nanobrain/docs/workflow_authoring_paths.md``
for the multi-path picker.

### ``Workflow.run(..., nest_under_active_context=True)`` — G31 runner-side

When invoking a workflow as a **nested sub-workflow** of another
workflow, set ``nest_under_active_context=True`` on the inner ``run()``
call. The framework auto-installs a nested ``WorkflowRunContext`` whose
namespace derives from the outer parent via ``derive_nested_namespace``
(strategy: ``"scoped"`` default, ``"inherit"`` opt-in via
``WorkflowConfig.namespace_strategy``).

```python
# Inside a parent step that invokes a child workflow:
result = await child_workflow.run(
    {"query": "..."},
    nest_under_active_context=True,
)
# child_workflow's data units, capability checks, and provenance
# all key off "<parent_namespace>.<child_workflow_name>" automatically.
```

When False (default): no behavior change; existing top-level callers
unaffected. When True without an outer context: warns + falls through
to non-nested run.

### Capability-token enforcement (G28)

Workflows that load tools with ``requires_capability: [...]`` now have
those tokens enforced at ``ToolExecutionStep.process()`` time. The
runner / caller populates ``WorkflowRunContext.capability_tokens`` on
the active context BEFORE invoking the workflow:

```python
ctx = WorkflowRunContext.from_config({
    "run_id": "abc",
    "capability_tokens": ["hpc.submit", "data.read"],
})
with ctx.activate():
    await workflow.run(...)
```

Missing tokens → ``CapabilityNotGranted`` workflow-terminal exception.

### Cost envelope enforcement (G26)

Long workflows can declare cumulative cost caps via ``CostEnvelope`` +
``CostTracker``. Cost-emitting code paths (LLM client, tool dispatch)
call ``record_cost(kind, amount)`` which checks against the active
tracker's caps:

```python
from nanobrain.core.cost_envelope import CostEnvelope, CostTracker

tracker = CostTracker(CostEnvelope(usd=10.0, tokens=1_000_000))
with tracker.activate():
    await workflow.run(...)  # any record_cost call past the cap raises
```

### Step-level events (G37)

Subscribe to live step lifecycle events (``step_start`` / ``step_complete``
/ ``step_failed``) for dashboards / log shippers / provenance recorders:

```python
from nanobrain.core.step_events import subscribe_to_step_events

events = []
with subscribe_to_step_events(events.append):
    await workflow.run(...)
# events is now a list[StepEvent] with one event per step boundary
```
