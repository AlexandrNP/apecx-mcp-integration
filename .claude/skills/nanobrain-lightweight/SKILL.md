---
name: nanobrain-lightweight
description: When and how to use the lightweight nanobrain authoring path (`nanobrain.lightweight.WorkflowBuilder` + auto-discovery) instead of hand-writing YAML or driving the apecx-mcp composer. Read whenever you need to prototype a workflow quickly, when you don't yet know the DAG shape, or when you want to programmatically generate YAML for many similar workflows.
---

# nanobrain-lightweight

## What "lightweight" actually is

`nanobrain/lightweight/` is **an ergonomic YAML generator**, not a separate
runtime. The builder API auto-discovers framework components, validates basic
shape, and **emits a framework-compatible YAML** that the normal
`Workflow.from_config()` then loads. There is no second runtime.

```python
from nanobrain.lightweight.enhanced_workflow_builder import EnhancedWorkflowBuilder

builder = EnhancedWorkflowBuilder("my_workflow", "Example")
builder.add_input("user_query", "DataUnitString")
builder.add_component("agent", "EnhancedCollaborativeAgent", model="gpt-4")
builder.add_output("response", "DataUnitString")
builder.connect("user_query", "agent")
builder.connect("agent", "response")
builder.save_workflow("my_workflow.yml")    # ← writes a normal nanobrain YAML

# Then load + run as usual:
wf = Workflow.from_config("my_workflow.yml")
await wf.run({"user_query": "..."})
```

Code lives at `nanobrain/nanobrain/lightweight/` (14 files).
The builder's own README labels itself **"iteration 1 — basic functionality
only"**, so be aware: it's the right tool for the *authoring* boundary, not
yet a complete replacement for hand-authored YAMLs in production.

## Three legit ways to author a workflow — pick the right one

| Path | When to use | Cost |
|---|---|---|
| **Hand-authored YAML** (`composition/workflows/<name>/workflow.yml`) | Production workflows; code-reviewed; version-controlled; reproducibility-critical | Verbose; easy to omit `auto_transfer: true` and ship a silent-failure |
| **Lightweight builder** (`nanobrain.lightweight.WorkflowBuilder`) | Rapid prototyping; exploratory work; programmatically generating many similar workflows from a template; when DAG shape is in flux | Iteration-1 maturity; less than full framework feature coverage; produces YAML that still must be reviewed |
| **apecx-mcp composer** (LLM-authored from natural language; see `agent_workflow_authoring.md`) | Scientist-driven workflow authoring; orchestrator decomposes a query into a runnable workflow YAML | Requires composer config; runs through the 5-gate validation pipeline; produces a `lowered_yaml_hash` for replay |

**Decision rule:** Start with hand-authored YAML for any workflow you intend
to ship. Use the lightweight builder when you're in the "what should this
workflow even look like" phase. Use the composer when the user's request is
framed as natural language and you don't already know which skeleton to pick.

### Pre-execution validation bridge (LW, 2026-05-11)

The LLM composer pipeline runs `validate_workflow_against_framework()`
after `yaml.safe_load` and BEFORE `Workflow.from_config`, surfacing
inline-dict / unresolvable-class / TransformLink / dangling-link
violations as a structured payload. The lightweight builder didn't
get that surface for free — `WorkflowBuilder.load()` writes the
accumulated config to temp YAML and calls `Workflow.from_config`, so
any framework violation falls out as a raw exception.

> **Steps are file-path-only (G121 reverted 2026-05-22).** A Step's
> `config` must be a file path, never an inline dict
> (`ConfigBase._is_inline_config_supported` excludes `BaseStep`).
> `WorkflowBuilder.load()` handles this for you: it writes each step's
> accumulated fields to its own temp YAML and references it by path.
> Links and triggers stay inline (they ARE inline-tolerant). You do
> not author step YAML by hand when using the builder — but if you
> bypass `load()` and dump `get_config()` to a file yourself, the flat
> step entries (no `config:` key) will be silently skipped at load
> (see shape #4 below).

Use the bridge module to get the same structured surface for
programmatic builds:

```python
from nanobrain.lightweight.workflow_builder import WorkflowBuilder
from apecx_integration.composition.lightweight_validator import (
    validate_and_load,
)

builder = WorkflowBuilder(...)
builder.add_step(...)
workflow = validate_and_load(builder)   # raises WorkflowValidationError
                                        # with structured violations
                                        # on framework-rule misses
```

Pure validation (no load) is also available via
`validate_lightweight_builder(builder)`. The bridge duck-types on
`.get_config()`, so a future `EnhancedWorkflowBuilder` or custom
subclass works without code changes here.

## Discovery — what the lightweight builder finds

`nanobrain/lightweight/__init__.py` exposes:

```python
from nanobrain.lightweight import workflow, list_agents, list_steps, list_executors
```

`list_agents()` / `list_steps()` / `list_executors()` enumerate the framework
components the builder can wire. Per the README, ~54 real framework
components are auto-discovered: agents (`EnhancedCollaborativeAgent`,
`QueryAnalysisAgent`, …), steps (`ConversationalResponseStep`,
`QueryClassificationStep`, …), tools (`BVBRCTool`, `MMseqs2Tool`,
`MUSCLETool`, …), data units, executors.

If a component you need isn't discovered, the builder cannot wire it; fall
back to hand-authored YAML and reference the class explicitly.

## Silent-failure shapes inherited from the framework

The lightweight builder **does not protect** you from the dominant
silent-failure shapes — it generates YAML, and the same shapes apply:

1. **`auto_transfer: true` discipline.** Verify every `DirectLink` block in
   the generated YAML carries `auto_transfer: true`. The builder's connect()
   API should set this; verify before committing the generated file. If not,
   open a bug against `nanobrain.lightweight.WorkflowBuilder.connect()`.
2. **`extra: forbid` discipline.** The builder writes parameter dicts into
   `config:` blocks. A typo in a parameter name produces a YAML that loads
   silently using the default. The same workspace memory rule applies; the
   target component's `ConfigBase` must enforce it.
3. **DataUnit name matching.** The names you pass to `add_input()` /
   `add_output()` MUST match the keys your step's `process()` returns. The
   builder cannot verify this — it's a runtime contract.
4. **Flat step entry → 0 child steps (fixed in `load()`, 2026-05-22).**
   A step entry that carries `class` but NO `config:` key is silently
   skipped by `_resolve_nested_objects` (it instantiates only
   `class`+`config` pairs), so the workflow loads with ZERO child steps,
   raises nothing, and `run()` returns `{'status':'no_first_step'}`.
   `WorkflowBuilder.load()` now prevents this by writing per-step temp
   YAML files. The hazard only bites if you dump `get_config()` to a file
   and load it directly — don't; use `load()`. Regression coverage:
   `tests/unit/test_workflow_builder.py::TestBuilderLoadAndRun`.

Cross-reference `nanobrain-data-units-triggers-links` and
`nanobrain-step-authoring` skills for the underlying warnings.

## Verification recipe before shipping a builder-generated YAML

```bash
# 1. Verify the file loads through the framework:
.venv/bin/python -c "
from nanobrain.core.workflow import Workflow
wf = Workflow.from_config('my_workflow.yml')
print('Loaded:', wf.name)
"

# 2. Verify every DirectLink declares auto_transfer:
grep -A 2 'class:.*DirectLink' my_workflow.yml | grep -c 'auto_transfer: true'
# Count must equal the number of DirectLinks declared in the file.

# 3. Run a smoke pass through wait_for_cascade:
.venv/bin/python -c "
import asyncio
from nanobrain.core.workflow import Workflow
async def main():
    wf = Workflow.from_config('my_workflow.yml')
    await wf.process({'user_query': 'smoke'})
    await wf.wait_for_cascade(timeout=60.0, settle_ms=500)
    # Inspect output data units; assert non-empty.
asyncio.run(main())
"
```

## When NOT to use the lightweight builder

- Production-shipped workflows. Hand-author the YAML, code-review it.
- Workflows that need framework features outside the iteration-1 scope
  (e.g., custom triggers, complex predicate routing, executor-specific
  configuration). The builder's API may not expose them.
- Workflows authored by an LLM in response to a user query. Use the
  composer (`agent_workflow_authoring.md` Strategies A/B/C) — it has the
  5-gate validation pipeline and produces a content-addressed hash for
  reproducibility.

## Cross-references

- `nanobrain/nanobrain/lightweight/README.md` — module-level overview
- `nanobrain/nanobrain/lightweight/enhanced_workflow_builder.py` — primary
  authoring entry point
- `nanobrain/nanobrain/lightweight/discovery.py` — auto-discovery system
- `nanobrain-workflow-authoring` skill — the underlying YAML schema this
  builder targets
- `agent_workflow_authoring.md` (apecx design package) — the
  composer-driven path that operates above the lightweight builder

## Companion path: Workflow.from_skeleton (G9-completion)

Lightweight ``WorkflowBuilder`` is the IN-PYTHON programmatic path
(build a workflow object directly). For PARAMETERIZED workflows where
the shape is fixed and the holes get filled per-call, prefer
``Workflow.from_skeleton(skeleton, bindings)`` (G9-completion). Both
converge on the same ``from_config`` runtime; see
``nanobrain/docs/workflow_authoring_paths.md`` for the picker matrix.

Three legitimate paths, picked by use case:
  1. Hand-written YAML — ``Workflow.from_config(path)``
  2. Skeleton + bindings — ``Workflow.from_skeleton(...)``
  3. Lightweight WorkflowBuilder — programmatic in-Python

The eval_03 Tier 0-4 chain (2026-05-09 -> 2026-05-11) ships
``Workflow.from_skeleton`` as the agent-authoring ergonomic;
``WorkflowBuilder`` remains the dynamic-DAG path for runtime-computed
shapes.
