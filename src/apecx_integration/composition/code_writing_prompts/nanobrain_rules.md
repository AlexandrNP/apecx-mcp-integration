# Nanobrain framework rules — LLM-facing condensate (v2, positive-only)

Apply these rules when the task involves any nanobrain class
(`BaseStep`, `ToolBase`, `Workflow`, `BaseAgent`).

## 1. Inherit `from_config` — do not define one

`BaseStep`, `ToolBase`, `Workflow`, and `BaseAgent` already inherit
`from_config(config_or_path)`. Your subclass should NOT define a
`from_config` method. Loading happens via `BaseStep.from_config(path)`
called from outside the class.

## 2. Implement `process()`

```python
class MyStep(BaseStep):
    COMPONENT_TYPE = "my_step"

    async def process(self, input_data: dict, **kwargs) -> dict:
        # business logic
        return {"output": ...}
```

`process` is the only method to implement. No constructor, no
`from_config`, no `execute`. The method signature is
`async def process(self, input_data: dict, **kwargs) -> dict`.

## 3. Imports

```python
from nanobrain.core.step import BaseStep, StepConfig
from nanobrain.core.tool import ToolBase
from nanobrain.core.workflow import Workflow
from nanobrain.core.agent import BaseAgent
from nanobrain.core.data_unit import DataUnitMemory
from nanobrain.core.trigger import DataUnitChangeTrigger
from nanobrain.core.link import DirectLink, ConditionalLink
from nanobrain.lightweight import WorkflowBuilder  # programmatic only
```

These are the canonical import paths. There is no `nanobrain.utils`
or `nanobrain.helpers`. The `nanobrain.lightweight` package contains
`WorkflowBuilder` and discovery helpers.

## 4. StepConfig subclass with a custom field

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

The `_strip_framework_keys` validator removes the `class:` key that
the framework injects at load time. The `source_path` field is
populated by the loader.

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
        return {"above": [x for x in input_data["items"] if x > self._threshold]}
```

## 6. Workflow YAML — every DirectLink declares `auto_transfer: true`

```yaml
links:
  one:
    class: "nanobrain.core.link.DirectLink"
    config:
      link_type: direct
      source: step_a.out
      target: step_b.in
      auto_transfer: true
```

Without `auto_transfer: true`, the link does not move data.

## 7. Workflow YAML — step config is a file path

```yaml
steps:
  s1:
    class: "my_module.MyStep"
    config: "configs/my_step.yml"
```

The step's `config:` value is a path to a YAML file. DataUnit, Link,
and Trigger configs may be inline; step configs must be file paths.

## 8. Tool subclass

```python
from nanobrain.core.tool import ToolBase

class MyTool(ToolBase):
    async def execute(self, **kwargs) -> dict:
        return {"result": ...}
```

Same inheritance pattern as steps: implement `execute`, inherit
`from_config`.
