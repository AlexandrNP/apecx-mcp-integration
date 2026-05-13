# test_code for step_uppercase — runs in the same sandbox namespace
# as the candidate. The candidate is expected to define ``UpperStep``
# in __main__; we load it via BaseStep.from_config with a temp YAML
# that names ``__main__.UpperStep`` so the from_config code path
# (not direct constructor) does the work — matching the user's
# "from_config only" rule.

import asyncio  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from nanobrain.core.step import BaseStep  # noqa: E402

# Sanity: did the candidate even define UpperStep?
assert "UpperStep" in globals(), "candidate did not define UpperStep at module scope"

# Build a temp YAML so BaseStep.from_config resolves __main__.UpperStep.
with tempfile.TemporaryDirectory() as _td:
    _yaml = Path(_td) / "step.yml"
    _yaml.write_text("class: '__main__.UpperStep'\nname: upper_test\n")
    _step = BaseStep.from_config(str(_yaml))

# Real execution: process a real input through the real step.
_result = asyncio.run(_step.process({"text": "hello"}))
assert isinstance(_result, dict), f"non-dict result: {type(_result).__name__}"
assert _result.get("output") == "HELLO", f"got {_result!r}"

# Verify the candidate did NOT override execute() — framework forbids it.
_cls = globals()["UpperStep"]
assert "execute" not in _cls.__dict__, "UpperStep overrides execute() — forbidden by framework"
