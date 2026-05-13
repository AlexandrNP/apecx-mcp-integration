import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.step import BaseStep

assert "FilterPositiveStep" in globals()

with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s.yml"
    _yaml.write_text("class: '__main__.FilterPositiveStep'\nname: filter_pos\n")
    _step = BaseStep.from_config(str(_yaml))

_r = asyncio.run(_step.process({"items": [-1, 2, -3, 4, 0, 5]}))
assert _r.get("positive") == [2, 4, 5], f"got {_r!r}"

_empty = asyncio.run(_step.process({"items": []}))
assert _empty.get("positive") == [], f"empty list case failed: {_empty!r}"

_all_neg = asyncio.run(_step.process({"items": [-1, -2, -3]}))
assert _all_neg.get("positive") == [], f"all-neg case: {_all_neg!r}"
