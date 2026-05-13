## Reference: a correct nanobrain ToolBase subclass

```python
from nanobrain.core.tool import ToolBase


class AdderTool(ToolBase):
    async def execute(self, *, a: int, b: int) -> dict:
        return {"sum": a + b}
```

Notes:
- Inherits `ToolBase` directly. No constructor. No custom `from_config`.
- `execute` is async. Keyword-only arguments (`*`,) for safety. Returns a dict.
- The function is loaded via `ToolBase.from_config(path)` from outside the class.

Author your solution following the same shape. Validate inputs inside `execute` if the spec requires rejecting bad input (raise `ValueError` for unsafe expressions, etc.). Do not redefine `from_config`.
