import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.step import BaseStep

assert "DedupePreserveOrderStep" in globals()

with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s.yml"
    _yaml.write_text("class: '__main__.DedupePreserveOrderStep'\nname: dedupe_test\n")
    _step = BaseStep.from_config(str(_yaml))

_r = asyncio.run(_step.process({"items": [1, 2, 1, 3, 2, 4]}))
assert _r.get("unique") == [1, 2, 3, 4], f"got {_r!r}"

# Strings work too — set-based dedupe would also work here.
_strs = asyncio.run(_step.process({"items": ["a", "b", "a", "c", "b"]}))
assert _strs.get("unique") == ["a", "b", "c"], f"strs case: {_strs!r}"

_empty = asyncio.run(_step.process({"items": []}))
assert _empty.get("unique") == [], f"empty case: {_empty!r}"
