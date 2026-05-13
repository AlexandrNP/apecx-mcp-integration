import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.step import BaseStep

assert "WordCountStep" in globals()

with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s.yml"
    _yaml.write_text("class: '__main__.WordCountStep'\nname: wc_test\n")
    _step = BaseStep.from_config(str(_yaml))

assert asyncio.run(_step.process({"text": "hello world"})).get("count") == 2
assert asyncio.run(_step.process({"text": "  multiple   spaces  here  "})).get("count") == 3
assert asyncio.run(_step.process({"text": ""})).get("count") == 0
assert asyncio.run(_step.process({"text": "single"})).get("count") == 1
