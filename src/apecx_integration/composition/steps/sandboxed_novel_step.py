"""SandboxedNovelStep — run composer-generated "novel Python" INSIDE a hardened Docker sandbox.

The composer emits NEW step classes in a ``novel_python`` fence (the CLOSED-CLASS-rule escape hatch
for cases where no library component fits). That code is UNTRUSTED — importing it into the host MCP
process would run arbitrary attacker-influenced Python with the server's privileges. This step moves
that execution across a process/container boundary: instead of ``import``-ing the novel class, it
ships the source + the step's inputs into the T13b hardened sandbox (``build_docker_sandbox_command``:
``--network=none``, ``--read-only``, ``--cap-drop=ALL``, memory/cpus/pids caps, non-root user) and runs
the ALREADY-BUILT in-container harness (``_novel_step_container/_novel_step_job.py``, baked into the
image at ``/app/_novel_step_job.py``) which ``from_config``-builds the class and runs its ``process``.

Choreography mirrors ``pymol_sasa_tool.py::run_sasa`` (tempdir → job.json → hardened ``docker run``
under ``acquire_container_slot`` → read result.json back), but uses the T13b command builder + the
dedicated read-write ``/out`` mount (the input dir mounts read-only at ``/work``; only ``/out`` is
writable, so the sandboxed code cannot tamper with its own inputs).

Failure is FAIL-LOUD: a nonzero exit / missing result / ``{"ok": false}`` envelope raises RuntimeError.
The caller/executor owns the G127 degrade policy — this step never returns empty on failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.docker_sandbox import (
    SandboxConfig,
    build_docker_sandbox_command,
)
from apecx_integration.composition.runtime.container_admission import acquire_container_slot

# Mirrors ``DockerSandboxRunner``'s gate in docker_sandbox.py: the sandbox refuses to invoke Docker
# unless the operator explicitly opts in, so a CI run of the unit suite cannot accidentally spawn a
# container. Unit tests that reach the run path monkeypatch the container call AND set this to "1".
_EXECUTE_ENV_VAR = "APECX_T13B_SANDBOX_EXECUTE"

# The in-container harness path (baked into the sandbox image at build time). See
# ``_novel_step_container/_novel_step_job.py`` for the module + its Dockerfile.
_HARNESS_IN_CONTAINER = "/app/_novel_step_job.py"


class SandboxedNovelStepConfig(StepConfig):
    """Configuration for :class:`SandboxedNovelStep`.

    ``name`` is inherited from :class:`StepConfig` (do NOT redeclare via ``@dataclass`` — a dataclass
    subclass silently DROPS the inherited field; the pydantic subclass form preserves it).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Populated by ConfigBase.from_config (``setattr(instance, 'source_path', path)``); an
    # ``extra='forbid'`` config MUST declare it or the load raises "object has no field source_path".
    source_path: str | None = Field(default=None)

    novel_source: str = Field(
        description="Full text of the composer's novel-python module (defines the target BaseStep)."
    )
    target_class_name: str = Field(
        description="Name of the BaseStep subclass defined in ``novel_source`` to build + run."
    )
    sandbox_image: str = Field(
        default="apecx-novel-sandbox:1.0",
        description="Docker image (with the harness baked at /app/_novel_step_job.py) to run in.",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Wall-clock ceiling for the sandboxed container run.",
    )
    step_config: dict[str, Any] = Field(
        default_factory=dict,
        description="The novel step's own config_override, passed through to the in-container "
        "from_config (merged with ``name`` by the harness).",
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class SandboxedNovelStep(BaseStep):
    """Run an untrusted composer-generated step inside the hardened Docker sandbox. See module docstring."""

    COMPONENT_TYPE: str = "sandboxed_novel_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return SandboxedNovelStepConfig

    @classmethod
    def extract_component_config(cls, config: SandboxedNovelStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "novel_source": config.novel_source,
            "target_class_name": config.target_class_name,
            "sandbox_image": config.sandbox_image,
            "timeout_seconds": config.timeout_seconds,
            "step_config": config.step_config,
        }

    def _init_from_config(
        self,
        config: SandboxedNovelStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._novel_source: str = component_config["novel_source"]
        self._target_class_name: str = component_config["target_class_name"]
        self._sandbox_image: str = component_config["sandbox_image"]
        self._timeout_seconds: float = float(component_config["timeout_seconds"])
        self._step_config: dict[str, Any] = dict(component_config["step_config"])

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        # Gate FIRST (before any tempdir / argv work): refuse unless the operator opted in, mirroring
        # DockerSandboxRunner. This is what keeps a CI unit run from spawning a real container.
        if os.environ.get(_EXECUTE_ENV_VAR) != "1":
            raise RuntimeError(
                f"{_EXECUTE_ENV_VAR}=1 is required to execute SandboxedNovelStep {self.name!r}: "
                "the sandbox refuses to invoke Docker without this explicit opt-in so a CI/test run "
                "cannot accidentally run untrusted novel Python in a container."
            )

        job = {
            "novel_source": self._novel_source,
            "target_class_name": self._target_class_name,
            "step_name": self.name,
            "config": self._step_config,
            "input_data": input_data,
        }

        # Two separate tempdirs: the INPUT dir mounts read-only at /work (job.json), the OUTPUT dir
        # mounts read-write at /out (the harness writes result.json). Keeping them apart means the
        # sandboxed code cannot tamper with its own inputs.
        with (
            tempfile.TemporaryDirectory(prefix="apecx_novel_in_") as in_tmp,
            tempfile.TemporaryDirectory(prefix="apecx_novel_out_") as out_tmp,
        ):
            input_dir = Path(in_tmp)
            output_dir = Path(out_tmp)
            (input_dir / "job.json").write_text(json.dumps(job))

            argv = build_docker_sandbox_command(
                ["python", _HARNESS_IN_CONTAINER, "/work/job.json", "/out/result.json"],
                input_host_path=input_dir,
                output_host_path=output_dir,
                config=SandboxConfig(
                    image=self._sandbox_image, timeout_seconds=self._timeout_seconds
                ),
            )

            returncode, stderr = await self._run_container(argv)

            # The harness ALWAYS writes result.json (ok:true → exit 0; ok:false → exit 1, still with a
            # structured envelope). A MISSING result.json therefore means an infra-level crash (OOM,
            # kill, image/harness broken) with no structured envelope — that is the only case that
            # raises the generic "exited N / stderr" error. When result.json is present, its envelope
            # is authoritative (a nonzero exit accompanies the ok:false envelope below).
            result_path = output_dir / "result.json"
            if not result_path.exists():
                stderr = (
                    ("…(truncated)…\n" + stderr[-8000:]) if len(stderr) > 8000 else stderr
                ).strip()
                raise RuntimeError(
                    f"SandboxedNovelStep {self.name!r}: sandbox exited {returncode} with no "
                    f"result.json (infra crash — no structured envelope). stderr:\n{stderr}"
                )
            result = json.loads(result_path.read_text())

        if result.get("ok"):
            return result["output"]

        # {"ok": false, ...}: fail LOUD with the in-container traceback — the caller owns G127 degrade.
        raise RuntimeError(
            f"SandboxedNovelStep {self.name!r}: novel step raised "
            f"{result.get('error_type', 'Error')}: {result.get('note', '')}\n"
            f"{(result.get('traceback') or '')[-4000:]}"
        )

    async def _run_container(self, argv: list[str]) -> tuple[int, str]:
        """Spawn the hardened container under the process-wide slot and return (returncode, stderr).

        Isolated so unit tests can monkeypatch it without a real Docker daemon. The slot is held for
        the container's whole lifetime (open-endpoint exhaustion guard, shared with DockerSandboxRunner
        + PyMOL via ``acquire_container_slot``).
        """
        async with acquire_container_slot():
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                _, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_seconds
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(
                    f"SandboxedNovelStep {self.name!r}: sandbox exceeded "
                    f"{self._timeout_seconds:.0f}s timeout"
                ) from exc
        return proc.returncode, stderr_b.decode("utf-8", errors="replace")


__all__ = ["SandboxedNovelStep", "SandboxedNovelStepConfig"]
