# Nanobrain framework rules — LLM-facing condensate

Apply these rules when the task involves any nanobrain class
(`BaseStep`, `ToolBase`, `Workflow`, `BaseAgent`, `DataUnit*`,
`Trigger*`, `Link*`). Each rule states the behavior the framework
enforces and the canonical pattern to follow. Imperatives only —
no rationale, no examples beyond the minimal one needed.

## 1. Use the inherited `from_config` — do NOT define your own

`BaseStep`, `ToolBase`, `Workflow`, `BaseAgent` all inherit
`from_config(config_or_path)` from `FromConfigBase`. Calling
`cls(...)` directly raises `RuntimeError: Direct instantiation
of <Class> is prohibited`. If your subclass overrides `from_config`
and calls `cls(...)`, you trigger the same error.

```python
# ✅ CORRECT — inherit from_config
class MyStep(BaseStep):
    async def process(self, input_data, **kwargs):
        return {...}

# ❌ FORBIDDEN — calling cls() inside a custom from_config
class MyStep(BaseStep):
    @classmethod
    def from_config(cls, path):
        return cls(path)  # raises RuntimeError
```

## 2. Implement `process()`, never `execute()`

```python
class MyStep(BaseStep):
    async def process(self, input_data: dict, **kwargs) -> dict:
        # business logic here
        return {...}
```

Overriding `execute()` raises `ComponentConfigurationError` at
step initialization (FAIL-FAST).

## 3. Imports

```python
from nanobrain.core.step import BaseStep, StepConfig
from nanobrain.core.tool import ToolBase
from nanobrain.core.workflow import Workflow
from nanobrain.core.agent import BaseAgent
from nanobrain.core.data_unit import DataUnitMemory
from nanobrain.core.trigger import DataUnitChangeTrigger
from nanobrain.core.link import DirectLink, ConditionalLink
from nanobrain.lightweight import WorkflowBuilder  # only when building programmatically
```

There is no `nanobrain.utils`, no `nanobrain.helpers`. The
`nanobrain.lightweight` package contains only `WorkflowBuilder` and
discovery helpers — NOT core classes.

## 4. StepConfig subclass shape (when adding custom fields)

```python
from typing import Any
from pydantic import ConfigDict, Field, model_validator
from nanobrain.core.step import StepConfig

class MyStepConfig(StepConfig):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)
    threshold: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data
```

`extra="forbid"` is the workspace rule. The `_strip_framework_keys`
validator is required to drop the `class:` field the framework
injects at load time.

## 5. Wiring custom config into a step

```python
class MyStep(BaseStep):
    COMPONENT_TYPE = "my_step"

    @classmethod
    def _get_config_class(cls):
        return MyStepConfig

    @classmethod
    def extract_component_config(cls, config):
        return {**super().extract_component_config(config), "threshold": config.threshold}

    def _init_from_config(self, config, component_config, dependencies):
        super()._init_from_config(config, component_config, dependencies)
        self._threshold = component_config["threshold"]

    async def process(self, input_data, **kwargs):
        return {...}
```

## 6. Workflow YAML — DirectLink MUST set `auto_transfer: true`

```yaml
links:
  one:
    class: "nanobrain.core.link.DirectLink"
    config:
      link_type: direct
      source: step_a.out
      target: step_b.in
      auto_transfer: true   # mandatory; without it the link silently no-ops
```

## 7. Workflow YAML — step config must be a FILE PATH, not inline

```yaml
steps:
  s1:
    class: "my_module.MyStep"
    config: "configs/my_step.yml"   # path, not a dict
```

Inline `config: { ... }` raises `❌ FRAMEWORK VIOLATION: Inline dict
configuration not supported for <class>`. Library exceptions:
DataUnit, Link, Trigger configs can be inline.

## 8. Tool subclass shape

```python
from nanobrain.core.tool import ToolBase

class MyTool(ToolBase):
    async def execute(self, **kwargs) -> dict:
        return {...}
```

Same `from_config` inheritance rule applies. Do not redefine it.
