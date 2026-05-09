---
name: nanobrain-config-yaml
description: How to write YAML configuration files for nanobrain components. Covers the `class:` + `config:` pattern for nested references, environment variable interpolation, the file-path resolution order, the difference between `ConfigBase` (file-only) and inline-dict-tolerant configs (DataUnit/Link/Trigger), and the standard fields shared across all components. Read this whenever you create or edit a `*.yml` under a nanobrain project.
---

# nanobrain-config-yaml

## Why this skill exists

Nanobrain is a **YAML-first** framework. Every behavior — model selection,
prompts, executor backends, data unit wiring, link routing — is configurable.
Hardcoded values in Python code are a code smell and, for some categories
(prompts, especially), are explicitly forbidden. This skill is the YAML
authoring guide.

> **Pydantic discipline:** Every `ConfigBase` subclass MUST set
> `model_config = {"extra": "forbid"}` (or workspace memory
> `pydantic_extra_forbid_rule`). Without it, a YAML typo (`auto_transer:` instead
> of `auto_transfer:`) silently uses the field's default — which is the bug
> shape behind the dominant silent-failure (see `nanobrain-data-units-triggers-links`).
> Workspace policy treats this as a non-negotiable rule.

> **Future framework primitives** (proposed in `nanobrain_capability_gaps.md`)
> that you may see referenced in design docs but cannot import yet:
> `PromptTemplate` (G14), `UnifiedToolDescriptor` (G15), `ExecutionPlanConfig`
> (G16), `WorkflowRunner` (G21), `WorkflowEntryTrigger` / `EventTrigger` (G22).
> Until they ship, hand-rolled BaseModel + DataUnitMemory is the workaround.

## File:line ground truth

| Concern | File | Approx. line |
|---|---|---|
| YAML loaded via `yaml.safe_load` | `nanobrain/nanobrain/core/component_base.py` | 593–594 |
| `class:` auto-delegation | `nanobrain/nanobrain/core/component_base.py` | 597–604 |
| Path resolution strategies | `nanobrain/nanobrain/core/component_base.py` | 700–791 |
| `ConfigBase.from_config` (file-path-only) | `nanobrain/nanobrain/core/config/config_base.py` | ~700–760 |
| Recursive nested resolution `class:` + `config:` | `nanobrain/nanobrain/core/config/config_base.py` | ~827–1113 |
| Inline-dict whitelist | `nanobrain/nanobrain/core/config/config_base.py` | ~1115–1144 |

## The two YAML idioms

### Idiom 1: A component's own config file

```yaml
# config/my_step.yml — a Step config
name: data_preparation_step
description: "Local data preparation step"

input_data_units:
  raw_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "raw_input"
    persistent: false

output_data_units:
  prepared_data:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "prepared_data"
    persistent: false

triggers:
  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
    data_unit: "raw_input"

executor:
  class: "nanobrain.core.executor.LocalExecutor"
  config: "local_step_executor.yml"
```

Two top-level shapes appear here:

- **Inline component config** — `input_data_units.raw_input` carries its own
  fields directly (`class:`, `name:`, `persistent:`). Allowed because
  `DataUnit` accepts inline dicts.
- **`class:` + `config:` indirection** — `executor` references a separate file.
  Use this when the executor is shared across many steps, or when the executor
  config is large.

### Idiom 2: A workflow that references step configs by file

```yaml
# config/mixed_execution_workflow.yml
name: my_workflow
version: "2.0"

input_data_units:
  raw_input:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "raw_input"
    persistent: false

output_data_units:
  final_results:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: "final_results"
    persistent: false

executors:
  local_executor:
    executor_type: local
    name: local_executor
    max_workers: 2
    timeout: 60

steps:
  data_preparation:
    class: my_project.steps.DataPreparationStep
    config: config/data_preparation_step.yml
    executor: local_executor

  result_aggregation:
    class: my_project.steps.ResultAggregationStep
    config: config/result_aggregation_step.yml
    executor: local_executor

links:
  prep_to_aggregate:
    class: "nanobrain.core.link.DirectLink"
    config:
      source: "data_preparation.prepared_data"
      target: "result_aggregation.input"
      link_type: direct

execution:
  timeout: 600
  retry_attempts: 2
  parallel_execution: false
```

Here every step is an external file referenced by `class:` + `config:`. This
is the recommended structure for non-trivial workflows.

## The `class:` field at the top of a YAML — auto-delegation

If a YAML's top level has `class: "module.path.ClassName"`, then **any** call
like `BaseClass.from_config('that_file.yml')` will be **redirected** to
`ClassName.from_config(...)`. This happens at
`component_base.py:597–604`.

```yaml
# config/specialized_step.yml
class: "my_project.steps.MySpecializedStep"
name: special_one
description: ...
```

```python
# Both of these end up calling MySpecializedStep:
step = BaseStep.from_config('config/specialized_step.yml')      # delegated
step = MySpecializedStep.from_config('config/specialized_step.yml')  # direct
```

If you do **not** want delegation, omit the `class:` field. The framework will
use whichever class you call.

## Nested `class:` + `config:` references

Inside any value that the framework recognizes as a component reference, you
can either inline the component's fields OR use the indirection pattern:

```yaml
# inline (resolves directly)
executor:
  executor_type: local
  max_workers: 2

# class + config (separate file)
executor:
  class: "nanobrain.core.executor.LocalExecutor"
  config: "config/local_executor.yml"

# class + inline config dict (rare; valid for DataUnit/Link/Trigger)
trigger:
  class: "nanobrain.core.trigger.DataUnitChangeTrigger"
  config:
    data_unit: "raw_input"
```

The framework recursively resolves these at schema-build time
(`config_base.py:827–1113`).

## Environment variable interpolation

The docstrings show this syntax:

```yaml
model: "${MODEL_NAME:-gpt-3.5-turbo}"
api_key: "${OPENAI_API_KEY}"
debug: "${DEBUG_MODE:-false}"
```

**However**, `_load_yaml_file` uses plain `yaml.safe_load`, which does **not**
interpolate environment variables. Interpolation, if it works, is implemented
elsewhere as a post-processing step. **Verify before relying on it**: write a
trivial config with `${SOME_VAR:-default}` and load it; if `model` ends up as
the literal string `"${SOME_VAR:-default}"`, interpolation isn't active.

If interpolation is not active in your environment, fall back to:

```python
import os
yaml_text = Path('config/agent.yml').read_text()
yaml_text = os.path.expandvars(yaml_text)
config = yaml.safe_load(yaml_text)
```

…or pass values as kwargs to `from_config()`. **Do not** hardcode secrets.

## Path resolution rules (where YAML files are looked for)

When you write `from_config('config/foo.yml')`, the framework searches:

1. Absolute path — used as-is.
2. **Calling class's directory** (preferred) — `inspect.getfile(cls)`/`config/foo.yml`.
3. **Calling class's parent directory** — one level up from #2.
4. **Current working directory** — `Path.cwd()`/`config/foo.yml`.
5. **Workflow base** — only if the path contains `workflows/`.

**Convention to follow**: place each component's YAML config next to the
Python file that defines the class. Tests that run from any cwd will then
resolve correctly via strategy #2.

## ConfigBase subclasses are file-only

Most config classes (`AgentConfig`, `StepConfig`, `WorkflowConfig`,
`ExecutorConfig`, `ToolConfig`) are `ConfigBase` subclasses. Calling
`AgentConfig.from_config({...inline dict...})` raises:

```
❌ FRAMEWORK VIOLATION: AgentConfig.from_config ONLY accepts file paths.
```

The exceptions — classes that allow inline dict configs — are:
- `DataUnit` (and subclasses: `DataUnitMemory`, `DataUnitFile`, …)
- `Link` (and subclasses: `DirectLink`, `QueueLink`, …)
- `Trigger` (and subclasses: `DataUnitChangeTrigger`, `TimerTrigger`, …)

Why: these often appear inline inside a larger config (a step's
`input_data_units` map, for example), so requiring a separate file for each
would be unworkable.

## Standard fields most components share

| Field | Type | Default | Purpose |
|---|---|---|---|
| `name` | str | required | Component identifier (used in logs, references) |
| `description` | str | "" | Human-readable purpose |
| `class` | str | optional | Auto-delegation target for nested or top-level use |
| `config` | str/dict | optional | Path to a sub-config file, or inline dict |
| `auto_initialize` | bool | true | Auto-init on creation |
| `debug_mode` | bool | false | Verbose diagnostics |
| `enable_logging` | bool | true | Framework logging |
| `timeout` | float/int | 300 | Execution timeout (seconds) |

Component-specific fields (e.g., `input_data_units` for steps, `model` for
agents) are documented in the per-component skills.

## Pitfalls and how to avoid them

1. **Inline dict to a `ConfigBase` subclass.** Easy to do in a Python REPL or
   test setup. Always pass a path. If you really need to construct a config
   programmatically, write the dict to a temp YAML and load that.

2. **Forgot the `class:` field on a sub-component.** Without `class:`, the
   framework cannot know which class to instantiate. You'll see something like:
   ```
   KeyError: 'class'
   ```
   or schema validation failures with confusing messages. Always declare
   `class:` for nested references.

3. **Wrong dotted path.** Typos in `class: "nanobrain.core.data_unit.DataUnitMemory"`
   produce `ImportError: Cannot import class: ...`. Verify the import path by
   running `python -c "from nanobrain.core.data_unit import DataUnitMemory"`.

4. **Relative path that resolves to the wrong file.** If `config/foo.yml`
   exists in both the class directory and the cwd, strategy #2 wins. To force
   a specific file, pass an absolute path.

5. **Duplicate `name:` across components.** The framework uses `name` for
   logging and trigger registration; collisions cause hard-to-debug behavior
   where two components share state. Use unique names per workflow.

6. **Workflow YAMLs that mix step ownership.** Do not put a step's data unit
   into the workflow-level `input_data_units` unless that data unit really is
   the workflow's entry point. See `nanobrain-step-authoring` for the
   ownership rule.

## Quick template (copy and edit)

```yaml
# config/my_component.yml
class: "my_project.module.MyComponent"  # optional; remove if calling MyComponent directly

name: my_component
description: "What this component does"

# Component-specific fields go here; see the per-component skill for the schema.

# Common nested-component patterns:
executor:
  class: "nanobrain.core.executor.LocalExecutor"
  config: "config/local_executor.yml"
```

## Checklist before you commit

- [ ] Every YAML file lives next to the class it configures (or in a clearly
      labeled `config/` directory).
- [ ] No secret values are inlined; use `${VAR}` or pass as kwargs.
- [ ] Every nested component reference uses either inline fields (for
      DataUnit/Link/Trigger) or `class:` + `config:` indirection (for everything else).
- [ ] No duplicate `name:` values within a workflow.
- [ ] `class:` import paths verified with a quick Python import.
- [ ] If you used `class:` at the top, the auto-delegation behavior is what you
      want.
