"""Containerization-hardening (branch container-timeout-no-orphan): a timed-out PyMOL / MAFFT
container must NOT orphan — the fix ``docker kill``s it BY NAME (``--name <container_name>`` in the
run argv) so it can't keep running under ``--rm`` (which only removes a container AFTER it stops).

This is the MEANINGFUL, real-docker proof. It uses **OPTION (ii)** from the task brief: rather than
build the (slow) PyMOL/MAFFT image and drive a real timeout, it exercises the REAL kill-by-name
primitive directly —

    1. start a real, long-running container named ``apecx-pymol-ittest`` (hardened ``--rm``, same as
       the production argv),
    2. call the REAL apecx code ``PyMOLToolBackendAdapter._docker_kill("apecx-pymol-ittest")``,
    3. assert ``docker ps --filter name=apecx-pymol-ittest`` is EMPTY (container actually killed —
       no orphan).

Why option (ii) and not (i): building ``apecx-pymol``/``apecx-mafft`` in-session is minutes-slow and
the timeout drive is inherently racy; the kill-by-name primitive is the exact line that regressed and
it is exercised end-to-end here against a real Docker daemon in <1s. The container-NAMING half of the
fix (that the argv actually carries ``--name <name>``) is pinned by the pure unit tests
``test_pymol_artifact_resolution.test_pymol_docker_argv_names_container_for_killability`` and
``test_local_mafft_align_step.test_mafft_docker_argv_names_container``. MAFFT uses the identical
``docker kill <name>`` primitive inline (``local_mafft_align_step._run_mafft_container``); this test
proves that primitive really removes a named container.

A locally-present small image is used so the test needs no network (Docker-provisioned apecx hosts
already have ``apecx-pymol`` / ``apecx-mafft``); it pulls busybox only as a last resort.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from apecx_integration.composition.steps.pymol_sasa_tool import PyMOLToolBackendAdapter

pytestmark = pytest.mark.integration

_IT_CONTAINER = "apecx-pymol-ittest"


def _docker_daemon_reachable() -> bool:
    """docker on PATH is necessary but not sufficient — Docker Desktop may be installed but not
    running. Mirrors tests/integration/test_docker_sandbox_runtime.py."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5.0, check=False
            ).returncode
            == 0
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


def _pick_local_image() -> str | None:
    """Return a locally-present small image to run ``sleep`` in (no network), else pull busybox."""
    try:
        out = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    present = set(out.split())
    for cand in ("busybox:latest", "alpine:latest", "apecx-mafft:7.505", "apecx-pymol:3.1.0"):
        if cand in present:
            return cand
    # Last resort: pull the tiny busybox image.
    if (
        subprocess.run(
            ["docker", "pull", "busybox"], capture_output=True, timeout=120, check=False
        ).returncode
        == 0
    ):
        return "busybox"
    return None


def _running_named(name: str) -> str:
    return subprocess.run(
        ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout.strip()


@pytest.mark.skipif(not _docker_daemon_reachable(), reason="Docker daemon unreachable")
def test_docker_kill_removes_named_container_no_orphan():
    """The real ``_docker_kill`` primitive must remove a live container by name — the anti-orphan
    guarantee. Start a real ``--rm`` container, kill it by name via apecx code, assert it is gone
    from ``docker ps`` (no orphan)."""
    image = _pick_local_image()
    if image is None:
        pytest.skip("no local small image available and busybox pull failed (offline)")

    # Clean any stale container from a prior aborted run.
    subprocess.run(
        ["docker", "rm", "-f", _IT_CONTAINER], capture_output=True, timeout=15, check=False
    )
    try:
        # Same hardened shape as production: --rm + --name. Long sleep so it is definitely running.
        started = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", _IT_CONTAINER, image, "sleep", "120"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert started.returncode == 0, f"docker run failed: {started.stderr!r}"
        assert _running_named(_IT_CONTAINER) == _IT_CONTAINER, "container did not start running"

        # THE REAL apecx kill-by-name path.
        asyncio.run(PyMOLToolBackendAdapter._docker_kill(_IT_CONTAINER))

        # No orphan: the named container must be gone from the running set.
        assert _running_named(_IT_CONTAINER) == "", (
            f"container {_IT_CONTAINER!r} orphaned — still running after _docker_kill"
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", _IT_CONTAINER], capture_output=True, timeout=15, check=False
        )
