import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.step import BaseStep

assert "ConcatStep" in globals()

with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s.yml"
    _yaml.write_text("class: '__main__.ConcatStep'\nname: concat_test\n")
    _step = BaseStep.from_config(str(_yaml))

_r = asyncio.run(_step.process({"parts": ["a", "b", "c"], "sep": "-"}))
assert _r.get("joined") == "a-b-c", f"got {_r!r}"

# Edge case: empty list joins to empty string.
_empty = asyncio.run(_step.process({"parts": [], "sep": "-"}))
assert _empty.get("joined") == "", f"empty case failed: got {_empty!r}"

assert "execute" not in globals()["ConcatStep"].__dict__
