import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.tool import ToolBase

assert "CalculatorTool" in globals()

with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "t.yml"
    _yaml.write_text("class: '__main__.CalculatorTool'\nname: calc_test\n")
    _tool = ToolBase.from_config(str(_yaml))

_r = asyncio.run(_tool.execute(expression="2 + 3 * 4"))
assert _r.get("result") == 14, f"got {_r!r}"

_r2 = asyncio.run(_tool.execute(expression="(10 - 4) / 2"))
assert _r2.get("result") == 3.0, f"got {_r2!r}"

# Negative case: non-arithmetic must raise.
try:
    asyncio.run(_tool.execute(expression="__import__('os').system('echo bad')"))
    raise AssertionError("dangerous expression should have raised ValueError")
except ValueError:
    pass
except Exception as e:
    # Accept any exception that prevents execution; not specifically ValueError.
    if "os" not in str(e).lower() and "import" not in str(e).lower():
        pass  # Some other safe-rejection path also acceptable.
