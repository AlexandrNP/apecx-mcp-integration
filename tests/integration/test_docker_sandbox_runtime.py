"""T13b Docker-sandbox runtime integration tests.

Two groups:

1. **Guard tests** — verify the scaffold refuses to run when Docker
   isn't on ``$PATH`` or the execute-gate env var isn't set. These run
   on any machine, including CI. They do NOT invoke Docker.

2. **Live-sandbox test** — opt-in via ``APECX_T13B_SANDBOX_EXECUTE=1``
   AND Docker daemon reachable. Runs a trivial Python command inside
   the hardened container and asserts exit-code + captured stdout.
   Skipped automatically when either condition is absent.

**Brutal truth:** a "the container actually isolates" test would
attempt to open a TCP socket, open ``/etc/passwd``, and assert both
fail. That test is high-value but needs to live alongside a
controlled Docker environment (specific daemon version, specific
seccomp profile); adding it here without that controlled environment
risks false-greens. Phase-3 concern.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest
from apecx_integration.composition.docker_sandbox import (
    DockerSandboxRunner,
    SandboxConfig,
    SandboxNotAvailableError,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Guard tests — safe on any machine
# ---------------------------------------------------------------------------


def test_runner_raises_without_execute_env_var(monkeypatch):
    """Without APECX_T13B_SANDBOX_EXECUTE=1, run() must refuse —
    even when docker IS on PATH. Prevents accidental Docker
    invocation from a bare ``pytest`` run.
    """
    monkeypatch.delenv("APECX_T13B_SANDBOX_EXECUTE", raising=False)
    runner = DockerSandboxRunner()

    # Pretend docker is available so we can reach the env-var check
    # regardless of the test host.
    monkeypatch.setattr(
        "apecx_integration.composition.docker_sandbox._docker_on_path",
        lambda: True,
    )

    with pytest.raises(SandboxNotAvailableError, match="APECX_T13B_SANDBOX_EXECUTE"):
        asyncio.run(runner.run(["true"]))


def test_runner_raises_when_docker_not_on_path(monkeypatch):
    """Whether or not the execute-gate env var is set, absence of
    the docker binary must surface as SandboxNotAvailableError —
    not some unrelated FileNotFoundError from subprocess_exec."""
    monkeypatch.setenv("APECX_T13B_SANDBOX_EXECUTE", "1")
    monkeypatch.setattr(
        "apecx_integration.composition.docker_sandbox._docker_on_path",
        lambda: False,
    )
    runner = DockerSandboxRunner()
    with pytest.raises(SandboxNotAvailableError, match="docker binary not found"):
        asyncio.run(runner.run(["true"]))


# ---------------------------------------------------------------------------
# Live-sandbox test — opt-in
# ---------------------------------------------------------------------------


def _docker_daemon_reachable() -> bool:
    """Return True when ``docker info`` returns 0 within 5s.

    `docker` on $PATH is necessary but not sufficient — Docker Desktop
    may be installed but not running, leaving a working CLI that fails
    every command.
    """
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_OPT_IN_REASON = (
    "T13b live sandbox test is opt-in: set APECX_T13B_SANDBOX_EXECUTE=1 "
    "AND have a reachable Docker daemon (e.g. Docker Desktop running). "
    "This test pulls a ~40MB image on first run."
)


@pytest.mark.skipif(
    os.environ.get("APECX_T13B_SANDBOX_EXECUTE") != "1" or not _docker_daemon_reachable(),
    reason=_OPT_IN_REASON,
)
def test_sandbox_runs_trivial_python_command():
    """Full round-trip: build argv, invoke Docker, capture stdout."""
    runner = DockerSandboxRunner(
        SandboxConfig(
            image="python:3.12-slim",
            timeout_seconds=120.0,  # first run pulls the image
        ),
    )
    result = asyncio.run(
        runner.run(
            ["python", "-c", "print('t13b-ok')"],
        )
    )
    assert result.exit_code == 0, (
        f"sandbox exited {result.exit_code}\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert "t13b-ok" in result.stdout
    assert not result.killed_by_timeout
    assert result.duration_seconds > 0


@pytest.mark.skipif(
    os.environ.get("APECX_T13B_SANDBOX_EXECUTE") != "1" or not _docker_daemon_reachable(),
    reason=_OPT_IN_REASON,
)
def test_sandbox_kills_runaway_via_timeout():
    """A command that sleeps past the timeout must be killed and the
    result must report ``killed_by_timeout=True``. This is the
    first line of defense against resource-exhaustion (threat-model
    row 5) — without a functioning timeout + kill, a runaway
    container could sit burning CPU until the operator notices.
    """
    runner = DockerSandboxRunner(
        SandboxConfig(
            image="python:3.12-slim",
            timeout_seconds=2.0,
        ),
    )
    result = asyncio.run(
        runner.run(
            ["python", "-c", "import time; time.sleep(30)"],
        )
    )
    assert result.killed_by_timeout, (
        f"timeout did not fire; exit_code={result.exit_code}, "
        f"duration={result.duration_seconds:.2f}s"
    )
    # exit_code of a killed docker-run client is implementation-specific;
    # the contract we pin is just that duration stays close to the
    # timeout — not drifting into "slept all 30 seconds".
    assert result.duration_seconds < 10.0
