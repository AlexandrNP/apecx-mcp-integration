---
name: nanobrain-from-config
description: How to create nanobrain components correctly via the mandatory `from_config` pattern. Read this FIRST before authoring any nanobrain code (steps, workflows, agents, tools, data units, links, triggers, executors). Covers the call sequence inside `from_config()`, configuration file path resolution, the four supported input shapes (str/Path/dict/Pydantic), the `class:` auto-delegation trick, the `_allow_direct_instantiation` backdoor, and the verbatim error messages the framework raises on violations.
---

# nanobrain-from-config

## Why this skill exists

The nanobrain framework enforces **configuration-driven component creation**.
Direct constructors (`Class(name="x")`) are not just discouraged — they are
forbidden by the framework, which raises `RuntimeError` from `__init__`. Every
component you author must inherit from `FromConfigBase` and must be created via
`Class.from_config(config)`. This skill is the contract.

> **Adjacent silent-failure to be aware of:** the `from_config` path can succeed
> while every `DirectLink` in the loaded workflow is a runtime no-op (default
> `auto_transfer=False`). See `nanobrain-data-units-triggers-links` for the
> warning. `from_config` is necessary, not sufficient, for a working workflow.

> **Wrong-Python failure looks like missing deps.** A `ModuleNotFoundError`
> at `from_config` time is almost always wrong-Python (system anaconda picked
> over `.venv/bin/python`). See `nanobrain-testing-debugging` § Wrong Python
> interpreter for diagnosis.

## File:line ground truth (cite when in doubt)

| Concern | File | Approx. line |
|---|---|---|
| `FromConfigBase` class | `nanobrain/nanobrain/core/component_base.py` | 134 |
| `from_config()` method body | `nanobrain/nanobrain/core/component_base.py` | 287–675 |
| `__init__` prohibition | `nanobrain/nanobrain/core/component_base.py` | 860–865 |
| `_resolve_config_file_path` | `nanobrain/nanobrain/core/component_base.py` | 700–791 |
| `validate_config_schema` | `nanobrain/nanobrain/core/component_base.py` | 793–809 |
| `extract_component_config` (default) | `nanobrain/nanobrain/core/component_base.py` | 811–820 |
| `resolve_dependencies` (default) | `nanobrain/nanobrain/core/component_base.py` | 822–830 |
| `create_instance` (uses `__new__`) | `nanobrain/nanobrain/core/component_base.py` | 832–839 |
| `_init_from_config` (default) | `nanobrain/nanobrain/core/component_base.py` | 841–853 |
| `ConfigBase` (file-path-only) | `nanobrain/nanobrain/core/config/config_base.py` | ~700–760 |

Line numbers may drift across framework versions; if an exact line doesn't
match, search by symbol name. The semantics below are stable.

## Mental model

`Class.from_config(config)` runs **six phases** in order:

1. **Normalize input** to a dict (file path → YAML load; dict → use as-is;
   Pydantic model → `.model_dump()`).
2. **Class auto-detection.** If the YAML has a top-level `class: "module.path.ClassName"`
   field and that path differs from the called class, **the call is delegated** —
   `target_class.from_config(config_path, **kwargs)` is invoked and the original
   class is bypassed entirely. This is intentional but easy to miss.
3. **Config object creation.** Calls `cls._get_config_class()` (which the
   subclass MUST implement) and instantiates it via its own `from_config` method
   (or constructor for legacy classes).
4. **Schema validation.** `validate_config_schema(config_object)` checks that
   `REQUIRED_CONFIG_FIELDS` are present.
5. **Dependency resolution.** `extract_component_config()` then
   `resolve_dependencies()` produce the kwargs the instance needs.
6. **Instance creation via `__new__`.** `create_instance(...)` calls `cls.__new__(cls)`
   to bypass `__init__` (which would otherwise raise), then calls
   `_init_from_config(config, component_config, dependencies)`. Finally,
   `_post_config_initialization()` is the last hook.

## The four supported config shapes

```python
# 1) YAML file (recommended for production)
agent = ConversationalAgent.from_config('config/agent.yml')

# 2) pathlib.Path object
from pathlib import Path
agent = ConversationalAgent.from_config(Path('config/agent.yml'))

# 3) Inline dict (testing / smoke tests only — see exception below)
agent = ConversationalAgent.from_config({'name': 'test', 'model': 'gpt-4'})

# 4) Pydantic config model (advanced)
config_obj = AgentConfig.from_config('config/agent.yml')
agent = ConversationalAgent.from_config(config_obj)
```

**Exception for inline dicts:** `ConfigBase`-derived configs reject inline
dicts with the error
`❌ FRAMEWORK VIOLATION: {ClassName}.from_config ONLY accepts file paths.`
The classes that *do* accept inline dicts are `DataUnit`, `Link`, `Trigger`
(and their subclasses) — the framework whitelists them in
`_is_inline_config_supported` (`config_base.py` ~line 1115). For everything
else, **use a YAML file**.

## Path resolution order (relative paths)

`_resolve_config_file_path` tries strategies in this order until one exists:

1. **Absolute path** — used as-is (raises `FileNotFoundError` if it doesn't exist).
2. **Calling-class directory** — `inspect.getfile(cls)` parent + relative path.
3. **Calling-class parent directory** — one level up from #2 (lets you put
   shared configs adjacent to the package, not inside it).
4. **Current working directory** — `Path.cwd()` + relative path.
5. **Workflow base directory** — only if the relative path contains
   `workflows/`, search `nanobrain/library/workflows`, `library/workflows`,
   `workflows`.

If all strategies fail, the framework raises
`❌ CONFIGURATION FILE RESOLUTION FAILED: {config_file}` with every searched
path enumerated. Read the message — it tells you exactly which directories were
checked.

**Worked example.** `MyStep.from_config('config/data_step.yml')` where
`MyStep` is defined at `nanobrain/library/steps/my_step.py`:

1. Tries `nanobrain/library/steps/config/data_step.yml` (class dir).
2. Tries `nanobrain/library/config/data_step.yml` (class parent dir).
3. Tries `<cwd>/config/data_step.yml`.
4. Tries workflow base only if path contained `workflows/` (it didn't).
5. Raises with all three attempts listed.

## What you implement in a subclass

**Mandatory.** Your subclass must define:

```python
@classmethod
def _get_config_class(cls):
    """Return the Pydantic config class for this component type."""
    return MyComponentConfig
```

If you forget, the framework raises:

```
NotImplementedError: Subclass {YourClass} must implement _get_config_class()
to specify which config class to use. This is the ONLY component-specific
method required.
```

**Optional overrides.** Each has the same signature across all components:

```python
@classmethod
def extract_component_config(cls, config) -> Dict[str, Any]:
    # Default extracts: name, description, timeout, enable_logging
    base = super().extract_component_config(config)
    base['my_field'] = config.my_field
    return base

@classmethod
def resolve_dependencies(cls, component_config, **kwargs) -> Dict[str, Any]:
    # Default returns: enable_logging, debug_mode, framework_version
    deps = super().resolve_dependencies(component_config, **kwargs)
    deps['executor'] = kwargs.get('executor') or LocalExecutor.from_config('config/local.yml')
    return deps

def _init_from_config(self, config, component_config, dependencies) -> None:
    super()._init_from_config(config, component_config, dependencies)
    self.executor = dependencies['executor']
    self.my_state = {}

def _post_config_initialization(self) -> None:
    # Last hook — runs after _init_from_config. No-op by default.
    self.nb_logger.info(f"{self.name} ready")
```

**Do not override `__init__`.** If you must run code at construction time, put
it in `_init_from_config` or `_post_config_initialization`.

## The `class:` auto-delegation trick

If a YAML file declares its target class:

```yaml
# config/my_step.yml
class: "nanobrain.library.steps.specialized.MySpecializedStep"
name: my_step
description: ...
```

…then **any** call like `BaseStep.from_config('config/my_step.yml')` will
delegate to `MySpecializedStep.from_config(...)` and never invoke `BaseStep`'s
own `_init_from_config`. This is how factory-style loading works in nanobrain.

**Implication:** when authoring a config file, decide whether you want this
delegation. If yes, put `class:` at the top. If no (you want to control which
class loads it), omit `class:` and call the right class explicitly in code.

## The `_allow_direct_instantiation` backdoor (do not use)

`__init__` raises `RuntimeError` unless `cls._allow_direct_instantiation` is
truthy. The framework sets this flag temporarily inside
`ConfigBase._create_validated_instance` and immediately resets it. **Setting
this flag persistently on a subclass to bypass the framework is a violation of
workspace policy.** If you find code doing this, treat it as a bug.

## Verbatim error messages (recognize these)

When you see one of these, search this skill first — the fix is usually a
config-shape mistake.

```
RuntimeError: Direct instantiation of {ClassName} is prohibited.
Use: {ClassName}.from_config(config_file_or_object)
```
> You called `ClassName(...)`. Replace with `ClassName.from_config('path.yml')`.

```
❌ FRAMEWORK VIOLATION: {ClassName}.from_config ONLY accepts file paths.
   GIVEN: <class 'dict'>
   REQUIRED: str or Path object pointing to YAML configuration file
   NOTE: Only DataUnit, Link, Trigger classes support inline dict config
```
> You passed a dict to a `ConfigBase` subclass that requires a file. Save the
> dict to a YAML file and pass its path.

```
❌ CONFIGURATION FILE RESOLUTION FAILED: {config_file}
   CALLING CLASS: {module.ClassName}
   SEARCHED PATHS:
      1. ...
      2. ...
```
> Read the searched paths. Either move the file to one of them or pass an
> absolute path.

```
ComponentConfigurationError: {ClassName} missing required configuration fields:
['field_a', 'field_b']
```
> Add the listed fields to your YAML.

```
NotImplementedError: Subclass {YourClass} must implement _get_config_class()
to specify which config class to use.
```
> Add `@classmethod def _get_config_class(cls): return YourConfig` to your subclass.

## Common pitfalls

1. **Class auto-delegation surprise.** A YAML loaded by `BaseStep.from_config()`
   silently delegates to a different class because of a `class:` field at the top.
   The original class's `extract_component_config` and `resolve_dependencies`
   are never called. If you need them, drop the `class:` field or call the
   declared class directly.
2. **Path-resolution race.** If the same config filename exists in both the
   class directory and the cwd, the class directory wins. Duplicate config
   files in two places is a foot-gun.
3. **Inline dict to ConfigBase.** Easy to do in tests, blows up at runtime with
   `ONLY accepts file paths`. Either save the dict to a temp YAML or use a
   class that allows inline dicts (DataUnit, Link, Trigger).
4. **Forgot `_get_config_class`.** The error is descriptive; just add the
   classmethod returning your `Config` class.
5. **Tried to use `__init__` for setup.** Move the work to `_init_from_config`
   or `_post_config_initialization`.

## Checklist before you commit

- [ ] No `Class(...)` for any FromConfigBase subclass — only `Class.from_config(...)`.
- [ ] Every component config lives in a YAML file (or is a dict, only if the class
      is `DataUnit`/`Link`/`Trigger`).
- [ ] `_get_config_class` is defined on every subclass you authored.
- [ ] No `__init__` override.
- [ ] Relative config paths resolve through one of the documented strategies; if
      tests run from a different cwd, prefer the class-directory strategy
      (place configs next to the class).
- [ ] No `_allow_direct_instantiation` set to `True` on your code.

## New from_config-compatible primitives (2026-05-09 -> 2026-05-11)

The eval_03 Tier 0-4 chain shipped 14+ new framework primitives that
all follow the standard ``from_config`` pattern. Quick reference:

| Need | Class | Module |
|---|---|---|
| Per-run provenance recorder | ``ProvenanceContext`` | ``nanobrain.core.provenance`` |
| Skeleton → runnable workflow | ``Workflow.from_skeleton`` (G9-completion classmethod) | ``nanobrain.core.workflow`` |
| Local Python-callable tool dispatch | ``LocalParslAdapter`` (G11-completion) | ``nanobrain.library.tools.local_parsl_adapter`` |
| Generic HTTP tool dispatch | ``HTTPBackendAdapter`` (G38) | ``nanobrain.library.tools.http_backend_adapter`` |
| Deferred human approval gate | ``DeferredHITLStep`` (G27) | ``nanobrain.library.steps.deferred_hitl_step`` |
| Approval persistence | ``InMemoryApprovalStore`` / ``FileApprovalStore`` (G27) | ``nanobrain.library.runtime.approval_store`` |
| Workflow cost cap enforcement | ``CostTracker`` (G26) | ``nanobrain.core.cost_envelope`` |
| Versioned data-source manifest | ``DataSourceRegistry`` (G24) | ``nanobrain.library.runtime.data_source_registry`` |
| Prompt regression harness | ``PromptRegressionHarness`` (G25) | ``nanobrain.library.testing.prompt_regression`` |
| Per-run scope (run_id + tokens + namespace) | ``WorkflowRunContext`` (G13+G28) | ``nanobrain.library.orchestration.run_context`` |
| Workspace-root locator | ``locate_workflow_root`` (G40) | ``nanobrain.library.runtime.workspace_root`` |

Each follows the standard pattern:
``Component.from_config(path)`` OR ``Component.from_config(dict)``
(when the class permits inline-dict admission). Specialized primitives
override ``from_config`` to admit pluggable runtime kwargs
(e.g., ``ApprovalStore``, ``http_callable``) that cannot be loaded
from YAML alone — these are passed as ``from_config(path, kwarg=...)``.
