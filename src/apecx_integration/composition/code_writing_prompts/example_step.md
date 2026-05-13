## Reference: a correct nanobrain BaseStep subclass

```python
from nanobrain.core.step import BaseStep


class UpperStep(BaseStep):
    COMPONENT_TYPE = "upper_step"

    async def process(self, input_data: dict, **kwargs) -> dict:
        return {"output": input_data["text"].upper()}
```

Notes about this reference:
- Inherits `BaseStep` directly. No constructor. No `from_config` method (inherited).
- `process` is async, takes `input_data: dict`, returns a dict.
- `COMPONENT_TYPE` is set as a class attribute (optional but conventional).
- No `execute` override.

Author your solution following the same shape. Substitute your class name, your dict keys, and your transformation. Keep the method signature exactly as shown.
