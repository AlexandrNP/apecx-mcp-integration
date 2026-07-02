"""#1c Phase 1 — the in-container novel-step job harness (_novel_step_job.py).

Runs the harness as a SUBPROCESS in the current venv (which has nanobrain) against a benign novel
BaseStep + a failing one, asserting the result.json envelope. This verifies the harness logic (real
nanobrain from_config + process) WITHOUT the container image; the real docker e2e is a later phase."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

_HARNESS = (
    Path(__file__).resolve().parents[2]
    / "src/apecx_integration/composition/steps/_novel_step_container/_novel_step_job.py"
)

_BENIGN_SOURCE = textwrap.dedent(
    """
    from typing import Any
    from nanobrain.core.step import BaseStep, StepConfig

    class Doubler(BaseStep):
        COMPONENT_TYPE = "doubler"
        REQUIRED_CONFIG_FIELDS = ["name"]

        @classmethod
        def _get_config_class(cls):
            return StepConfig

        async def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
            return {"out": input_data.get("x", 0) * 2}
    """
)

_RAISING_SOURCE = textwrap.dedent(
    """
    from typing import Any
    from nanobrain.core.step import BaseStep, StepConfig

    class Boom(BaseStep):
        COMPONENT_TYPE = "boom"
        REQUIRED_CONFIG_FIELDS = ["name"]

        @classmethod
        def _get_config_class(cls):
            return StepConfig

        async def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("kaboom in novel step")
    """
)


def _run_harness(tmp_path: Path, job: dict) -> dict:
    (tmp_path / "job.json").write_text(json.dumps(job))
    result_path = tmp_path / "result.json"
    proc = subprocess.run(
        [sys.executable, str(_HARNESS), str(tmp_path / "job.json"), str(result_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result_path.exists(), f"harness wrote no result.json; stderr={proc.stderr[:500]}"
    return json.loads(result_path.read_text())


def test_harness_runs_benign_novel_step(tmp_path):
    out = _run_harness(
        tmp_path,
        {
            "novel_source": _BENIGN_SOURCE,
            "target_class_name": "Doubler",
            "step_name": "dbl",
            "config": {},
            "input_data": {"x": 7},
        },
    )
    assert out["ok"] is True, out
    assert out["output"] == {"out": 14}


def test_harness_emits_structured_error_on_raise(tmp_path):
    out = _run_harness(
        tmp_path,
        {
            "novel_source": _RAISING_SOURCE,
            "target_class_name": "Boom",
            "step_name": "boom",
            "config": {},
            "input_data": {},
        },
    )
    assert out["ok"] is False
    assert out["error_type"] == "ValueError"
    assert "kaboom" in out["note"]
    assert "Traceback" in out["traceback"]


def test_harness_reports_missing_class(tmp_path):
    out = _run_harness(
        tmp_path,
        {"novel_source": "x = 1\n", "target_class_name": "Nope", "config": {}, "input_data": {}},
    )
    assert out["ok"] is False
    assert out["error_type"] == "AttributeError"
