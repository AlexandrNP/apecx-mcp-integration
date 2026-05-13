import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.step import BaseStep

assert "DoubleStep" in globals(), "candidate did not define DoubleStep"

with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s.yml"
    _yaml.write_text("class: '__main__.DoubleStep'\nname: double_test\n")
    _step = BaseStep.from_config(str(_yaml))

_neg = asyncio.run(_step.process({"n": -7}))
assert isinstance(_neg, dict) and _neg.get("doubled") == -14, f"got {_neg!r}"

_pos = asyncio.run(_step.process({"n": 5}))
assert _pos.get("doubled") == 10, f"got {_pos!r}"

assert "execute" not in globals()["DoubleStep"].__dict__, "execute() override forbidden"
