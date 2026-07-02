"""#1c — REAL sandboxed execution of a novel step in the hardened container (docker-gated).

Runs the baked-in harness inside the `apecx-novel-sandbox` image under the real
`build_docker_sandbox_command` flags (--network none, --read-only, unprivileged, cap-drop ALL). Proves
the security boundary end-to-end: a benign novel step runs + returns output; network egress is blocked;
a failing step surfaces a structured traceback. Auto-skips without docker or the image.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from apecx_integration.composition.docker_sandbox import SandboxConfig, build_docker_sandbox_command

pytestmark = pytest.mark.integration

_IMAGE = "apecx-novel-sandbox:1.0"


def _image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


_SKIP = not _image_present()
_REASON = (
    f"docker or {_IMAGE} not available (build: docker build -t {_IMAGE} <_novel_step_container/>)"
)


def _run_in_sandbox(novel_source: str, target_class: str, input_data: dict) -> dict:
    with tempfile.TemporaryDirectory() as ind, tempfile.TemporaryDirectory() as outd:
        (Path(ind) / "job.json").write_text(
            json.dumps(
                {
                    "novel_source": novel_source,
                    "target_class_name": target_class,
                    "step_name": "novel",
                    "config": {},
                    "input_data": input_data,
                }
            )
        )
        argv = build_docker_sandbox_command(
            ["python", "/app/_novel_step_job.py", "/work/job.json", "/out/result.json"],
            input_host_path=Path(ind),
            output_host_path=Path(outd),
            config=SandboxConfig(image=_IMAGE),
        )
        subprocess.run(argv, capture_output=True, text=True, timeout=120)
        return json.loads((Path(outd) / "result.json").read_text())


_BENIGN = textwrap.dedent(
    """
    from typing import Any
    from nanobrain.core.step import BaseStep, StepConfig
    class Doubler(BaseStep):
        COMPONENT_TYPE = "doubler"
        REQUIRED_CONFIG_FIELDS = ["name"]
        @classmethod
        def _get_config_class(cls): return StepConfig
        async def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
            return {"out": input_data.get("x", 0) * 2}
    """
)

_EGRESS = textwrap.dedent(
    """
    from typing import Any
    from nanobrain.core.step import BaseStep, StepConfig
    class Egress(BaseStep):
        COMPONENT_TYPE = "egress"
        REQUIRED_CONFIG_FIELDS = ["name"]
        @classmethod
        def _get_config_class(cls): return StepConfig
        async def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
            import urllib.request
            urllib.request.urlopen("http://example.com", timeout=5)  # blocked by --network none
            return {"reached": True}
    """
)

_RAISER = textwrap.dedent(
    """
    from typing import Any
    from nanobrain.core.step import BaseStep, StepConfig
    class Boom(BaseStep):
        COMPONENT_TYPE = "boom"
        REQUIRED_CONFIG_FIELDS = ["name"]
        @classmethod
        def _get_config_class(cls): return StepConfig
        async def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("kaboom in sandbox")
    """
)


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_benign_novel_step_runs_in_container():
    out = _run_in_sandbox(_BENIGN, "Doubler", {"x": 9})
    assert out["ok"] is True, out
    assert out["output"] == {"out": 18}


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_novel_step_network_egress_blocked():
    out = _run_in_sandbox(_EGRESS, "Egress", {})
    # The code RAN (so this is a real egress attempt) but --network none makes it fail, NOT reach out.
    assert out["ok"] is False, out
    assert "reached" not in json.dumps(out)


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_novel_step_failure_surfaces_traceback():
    out = _run_in_sandbox(_RAISER, "Boom", {})
    assert out["ok"] is False
    assert out["error_type"] == "ValueError"
    assert "kaboom" in out["note"]
    assert "Traceback" in out["traceback"]
