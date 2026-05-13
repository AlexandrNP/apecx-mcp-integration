## Reference: a nanobrain step with a custom StepConfig field

```python
from typing import Any
from pydantic import ConfigDict, Field, model_validator
from nanobrain.core.step import BaseStep, StepConfig


class ThresholdStepConfig(StepConfig):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)
    threshold: float = Field(default=0.0)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class ThresholdStep(BaseStep):
    COMPONENT_TYPE = "threshold_step"

    @classmethod
    def _get_config_class(cls):
        return ThresholdStepConfig

    @classmethod
    def extract_component_config(cls, config):
        return {**super().extract_component_config(config), "threshold": config.threshold}

    def _init_from_config(self, config, component_config, dependencies):
        super()._init_from_config(config, component_config, dependencies)
        self._threshold = component_config["threshold"]

    async def process(self, input_data, **kwargs):
        return {"above": [x for x in input_data["items"] if x > self._threshold]}
```

Notes:
- The **`_strip_framework_keys`** `model_validator` is REQUIRED because the framework injects `class:` into the config dict at load time; without the validator, `extra='forbid'` rejects it.
- `source_path` field is required for the framework's tracking.
- `_get_config_class`, `extract_component_config`, and `_init_from_config` are the three wiring methods. Do not skip any.
- Read the custom field (`threshold`) inside `_init_from_config` and use it in `process`.

Author your solution following the same shape with your custom field name and type.
