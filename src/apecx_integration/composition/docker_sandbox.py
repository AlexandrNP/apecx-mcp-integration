"""T13b — runtime Docker sandbox (scaffold).

Phase-2 runtime isolation backstop for T13's static import-whitelist
scanner (``sandbox.py``). See ``docs/t13b_sandbox_design.md`` for the
threat model + flag rationale.

**This module is a scaffold.** The ``build_docker_sandbox_command``
argv construction is complete and unit-tested; the runtime
``DockerSandboxRunner.run`` exists but is gated behind the
``APECX_T13B_SANDBOX_EXECUTE`` env var to prevent accidental Docker
invocation in CI. Composer wiring to actually USE this runner is
Phase-3 work.

API:

    from apecx_integration.composition.docker_sandbox import (
        DockerSandboxRunner,
        SandboxConfig,
        SandboxResult,
        SandboxNotAvailableError,
        build_docker_sandbox_command,
    )
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Configuration + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SandboxConfig:
    """Tunable isolation parameters.

    Defaults are the threat-model-justified values from
    ``docs/t13b_sandbox_design.md``. Override judiciously — weakening
    any field requires a matching update to the threat-model table.
    """

    image: str = "python:3.12-slim"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 256
    user: str = "65534:65534"  # nobody:nogroup
    network: str = "none"
    tmpfs_size: str = "256m"
    workdir: str = "/work"
    timeout_seconds: float = 60.0
    extra_run_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True)
class SandboxResult:
    """Outcome of a single sandboxed execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    killed_by_timeout: bool
    argv: tuple[str, ...]  # the exact docker run invocation (for audit)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SandboxError(Exception):
    """Base class for sandbox errors."""


class SandboxNotAvailableError(SandboxError):
    """Raised when the sandbox cannot be invoked.

    Two known causes:
    - ``docker`` binary is not on ``$PATH``.
    - ``APECX_T13B_SANDBOX_EXECUTE`` env var is not set. The scaffold
      requires this explicit opt-in so that CI runs of the test suite
      cannot accidentally invoke Docker on a developer's host.
    """


# ---------------------------------------------------------------------------
# Command construction — pure, no I/O, fully unit-testable
# ---------------------------------------------------------------------------


def build_docker_sandbox_command(
    command: Sequence[str],
    *,
    input_host_path: Path | None,
    container_name: str | None = None,
    config: SandboxConfig | None = None,
) -> list[str]:
    """Construct the ``docker run`` argv for a hardened sandbox invocation.

    Every flag corresponds to a row in the threat-model table in
    ``docs/t13b_sandbox_design.md``. If you remove or weaken a flag
    here, update that table and the unit tests in lockstep.

    Args:
        command: The command to run inside the container, e.g.
            ``["python", "script.py"]``. Interpreted relative to
            ``config.workdir``.
        input_host_path: Host directory to bind-mount **read-only**
            at ``config.workdir`` inside the container. ``None``
            means no input mount (for smoke-testing the container
            itself).
        container_name: Optional explicit container name. If None,
            Docker will auto-generate one. Passing a name is useful
            when the caller wants to ``docker kill <name>`` on
            cancellation; Phase-3 will wire that path.
        config: Isolation parameters. ``None`` uses the defaults.

    Returns:
        The argv (``list[str]``) ready to pass to ``subprocess.run``.
    """
    cfg = config or SandboxConfig()

    argv: list[str] = [
        "docker", "run",
        "--rm",
        "--network", cfg.network,
        "--read-only",
        "--tmpfs", f"/tmp:size={cfg.tmpfs_size},mode=1777",
        "--memory", f"{cfg.memory_mb}m",
        # --memory-swap equal to --memory disables swap. Without this,
        # the memory cap is unenforced whenever swap is available.
        "--memory-swap", f"{cfg.memory_mb}m",
        "--cpus", str(cfg.cpus),
        "--pids-limit", str(cfg.pids_limit),
        "--user", cfg.user,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--security-opt", "seccomp=default",
        "--workdir", cfg.workdir,
    ]

    if input_host_path is not None:
        # resolve() + readonly. Path must be absolute on the host; Docker
        # rejects relative bind sources.
        src = input_host_path.resolve()
        argv += [
            "--mount",
            f"type=bind,source={src},target={cfg.workdir},readonly",
        ]

    if container_name is not None:
        argv += ["--name", container_name]

    argv += list(cfg.extra_run_args)
    argv.append(cfg.image)
    argv += list(command)
    return argv


# ---------------------------------------------------------------------------
# Runtime wrapper — gated, opt-in
# ---------------------------------------------------------------------------


_EXECUTE_ENV_VAR = "APECX_T13B_SANDBOX_EXECUTE"


def _docker_on_path() -> bool:
    return shutil.which("docker") is not None


class DockerSandboxRunner:
    """Runs commands inside a hardened Docker sandbox.

    Scaffold caveats — this class is deliberately under-featured:
    - No cancellation path (ask the container to die via
      ``docker kill``). Phase-3.
    - No ``/out`` writable-mount convention. Phase-3.
    - No image-pin-by-digest enforcement. Phase-3.

    The intent today is to lock in the argv shape (via
    ``build_docker_sandbox_command``) and make it cheap for Phase-3
    to add the missing pieces without reshaping the API.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    @property
    def config(self) -> SandboxConfig:
        return self._config

    def _check_available(self) -> None:
        if not _docker_on_path():
            raise SandboxNotAvailableError(
                "docker binary not found on $PATH; install Docker Desktop "
                "or the Docker CLI before invoking DockerSandboxRunner."
            )
        if os.environ.get(_EXECUTE_ENV_VAR) != "1":
            raise SandboxNotAvailableError(
                f"{_EXECUTE_ENV_VAR}=1 is required to execute the sandbox. "
                "This explicit opt-in exists so CI runs of the test suite "
                "do not accidentally invoke Docker. Unit tests of argv "
                "construction call build_docker_sandbox_command directly "
                "and do not trip this guard."
            )

    async def run(
        self,
        command: Sequence[str],
        *,
        input_host_path: Path | None = None,
        container_name: str | None = None,
    ) -> SandboxResult:
        """Execute ``command`` inside the sandbox.

        Refuses to run unless ``APECX_T13B_SANDBOX_EXECUTE=1`` is set.
        """
        self._check_available()
        argv = build_docker_sandbox_command(
            command,
            input_host_path=input_host_path,
            container_name=container_name,
            config=self._config,
        )

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        killed = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            killed = True
            # Ask the container to die. ``--rm`` takes care of removal.
            if container_name is not None:
                # Best-effort kill; fire-and-forget (we're already timing
                # out, don't block further).
                try:
                    subprocess.run(
                        ["docker", "kill", container_name],
                        check=False,
                        timeout=5.0,
                        capture_output=True,
                    )
                except Exception:
                    pass
            # Kill the docker-run client process itself, whatever state
            # it's in. Without this, communicate() below can still hang.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            stdout_b, stderr_b = await proc.communicate()

        duration = time.monotonic() - start
        returncode = proc.returncode if proc.returncode is not None else -1

        return SandboxResult(
            exit_code=returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_seconds=duration,
            killed_by_timeout=killed,
            argv=tuple(argv),
        )


__all__ = [
    "DockerSandboxRunner",
    "SandboxConfig",
    "SandboxError",
    "SandboxNotAvailableError",
    "SandboxResult",
    "build_docker_sandbox_command",
]
