import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.step import BaseStep

assert "SumListStep" in globals()

with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s.yml"
    _yaml.write_text("class: '__main__.SumListStep'\nname: sum_test\n")
    _step = BaseStep.from_config(str(_yaml))

_r = asyncio.run(_step.process({"values": [1.0, 2.5, 3.5]}))
assert abs(_r.get("total", 0) - 7.0) < 1e-9, f"got {_r!r}"

_empty = asyncio.run(_step.process({"values": []}))
assert _empty.get("total") == 0, f"empty case: {_empty!r}"

_neg = asyncio.run(_step.process({"values": [-1.5, -2.5]}))
assert abs(_neg.get("total", 999) - (-4.0)) < 1e-9, f"neg case: {_neg!r}"
