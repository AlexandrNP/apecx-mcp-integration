"""#1c capstone — a composed workflow with a novel step LOADS + RUNS through the full chain.

Before #1c a novel step's bare `class:` made Workflow.from_config raise -> load_failed. This ties the
whole chain together: expand_spec (routes novel -> SandboxedNovelStep) -> LocalExecutor._stage_workflow
(materializes steps/<id>.yml + strips the metadata key) -> Workflow.from_config (loads the proxy) ->
SandboxedNovelStep.process (runs the novel step in the REAL hardened container).

test_...loads is docker-FREE (proves the expander+stager+from_config wiring cohere). test_...runs is
docker+image-gated (the real container leg). Auto-skips without docker/the image.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import textwrap

import pytest
import yaml

from apecx_integration.composition.steps.sandboxed_novel_step import SandboxedNovelStep
from apecx_integration.composition.workflow_spec import (
    MinimalWorkflowSpec,
    WorkflowStepSpec,
    expand_spec,
)
from apecx_integration.control_plane.executors.local import LocalExecutor

_IMAGE = "apecx-novel-sandbox:1.0"

_DOUBLER = textwrap.dedent(
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


class _StubExec:
    def __init__(self, base):
        self._workflow_base_dir = base


def _compose_and_stage(tmp_path):
    spec = MinimalWorkflowSpec(
        name="novel_wf",
        steps=[WorkflowStepSpec(id="dbl", class_name="Doubler")],
        novel_python={"dbl": _DOUBLER},
    )
    wf_dict, _ = expand_spec(spec, [])
    base = tmp_path / "base"
    (base / "steps").mkdir(parents=True)
    artifact = tmp_path / "artifact.yml"
    artifact.write_text(yaml.safe_dump(wf_dict))
    run_root = tmp_path / "run"
    run_root.mkdir()
    return LocalExecutor._stage_workflow(_StubExec(base), artifact, run_root), run_root


def _find_sandboxed_step(wf) -> SandboxedNovelStep:
    # Robust across the Workflow step-container attr name.
    for attr in ("steps", "child_steps", "_steps"):
        container = getattr(wf, attr, None)
        if isinstance(container, dict):
            for s in container.values():
                if isinstance(s, SandboxedNovelStep):
                    return s
    raise AssertionError(f"no SandboxedNovelStep found on the loaded workflow ({type(wf)})")


def test_composed_novel_workflow_loads_via_from_config(tmp_path):
    # The core wiring proof — docker-free. Before #1c this raised (load_failed).
    from nanobrain.core.workflow import Workflow

    staged, run_root = _compose_and_stage(tmp_path)
    # The stager materialized the novel step's file-path config + stripped the metadata key.
    assert (run_root / "steps" / "dbl.yml").is_file()
    assert "_apecx_sandboxed_novel_config" not in yaml.safe_load(staged.read_text())

    wf = Workflow.from_config(str(staged))
    step = _find_sandboxed_step(wf)
    assert step._target_class_name == "Doubler"
    assert "class Doubler" in step._novel_source


def _docker_image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", _IMAGE], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _docker_image_present(), reason=f"docker or {_IMAGE} not available")
def test_loaded_sandboxed_step_runs_novel_code_in_real_container(tmp_path, monkeypatch):
    from nanobrain.core.workflow import Workflow

    staged, _ = _compose_and_stage(tmp_path)
    wf = Workflow.from_config(str(staged))
    step = _find_sandboxed_step(wf)

    monkeypatch.setenv("APECX_T13B_SANDBOX_EXECUTE", "1")
    out = asyncio.run(step.process({"x": 7}))
    assert out == {"out": 14}  # the novel Doubler ran in the real hardened container
