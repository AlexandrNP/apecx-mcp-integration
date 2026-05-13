## Reference: a nanobrain workflow built programmatically via WorkflowBuilder

```python
from nanobrain.core.step import BaseStep
from nanobrain.lightweight import WorkflowBuilder


class UpperStep(BaseStep):
    COMPONENT_TYPE = "upper"
    async def process(self, input_data, **kwargs):
        return {"text": input_data["text"].upper()}


class ReverseStep(BaseStep):
    COMPONENT_TYPE = "reverse"
    async def process(self, input_data, **kwargs):
        return {"text": input_data["text"][::-1]}


def build_workflow():
    b = WorkflowBuilder("u_then_r")
    upper_dus = {
        "input_data_units": {
            "upper_input": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "upper_input"},
        },
        "output_data_units": {
            "upper_output": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "upper_output"},
        },
        "triggers": [{"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "upper_input"}],
    }
    reverse_dus = {
        "input_data_units": {
            "reverse_input": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "reverse_input"},
        },
        "output_data_units": {
            "reverse_output": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "reverse_output"},
        },
        "triggers": [{"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "reverse_input"}],
    }
    b.add_step("upper", "__main__.UpperStep", **upper_dus)
    b.add_step("reverse", "__main__.ReverseStep", **reverse_dus)
    b.add_link("upper.upper_output", "reverse.reverse_input", link_type="direct")
    return b.load()
```

Notes:
- Each step needs `input_data_units`, `output_data_units`, and at least one `triggers` entry (`DataUnitChangeTrigger`) so the cascade fires.
- Use `__main__.YourClass` as the dotted class path so the framework finds candidate-defined classes.
- `add_link(..., link_type="direct")` injects `auto_transfer: true` automatically under `config_version: 2`.
- `builder.load()` returns a real `Workflow` instance.

Author your solution following the same shape with your step classes and link wiring.
