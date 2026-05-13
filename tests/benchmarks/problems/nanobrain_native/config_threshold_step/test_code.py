import asyncio
import tempfile
from pathlib import Path

from nanobrain.core.step import BaseStep

assert "ThresholdStep" in globals()
assert "ThresholdStepConfig" in globals()

# Configure threshold=5.
with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s.yml"
    _yaml.write_text("class: '__main__.ThresholdStep'\nname: threshold_test\nthreshold: 5.0\n")
    _step = BaseStep.from_config(str(_yaml))

_r = asyncio.run(_step.process({"items": [1, 5, 6, 10, 3, 7]}))
assert _r.get("above") == [6, 10, 7], f"threshold=5 got {_r!r}"

# Default threshold = 0.
with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "s2.yml"
    _yaml.write_text("class: '__main__.ThresholdStep'\nname: threshold_default\n")
    _step2 = BaseStep.from_config(str(_yaml))

_r2 = asyncio.run(_step2.process({"items": [-2, -1, 0, 1, 2]}))
assert _r2.get("above") == [1, 2], f"default threshold got {_r2!r}"

assert "execute" not in globals()["ThresholdStep"].__dict__
