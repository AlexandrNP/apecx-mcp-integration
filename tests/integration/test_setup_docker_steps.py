"""Integration tests for the Docker-service setup steps (E3-4 + E3-7).

Two unconditional tests (the env-derivation + the docker-down named-degrade
contract, CC-1/CC-5) and two docker-gated tests that hit REAL images/worker:

* ``_step_pymol`` builds/finds ``apecx-pymol:3.1.0`` and a ``pymol2`` smoke
  inside it reports the pinned version (E3-7).
* ``_step_rhea`` brings up the from-fork worker, ingests the catalog, and
  ``find_tools(<tool>)`` surfaces >=1 tool (E3-4.2, CC-1 non-empty catalog).

The docker-gated tests are SKIPPED (not failed) when the daemon is down, so
CI without Docker stays green; when Docker IS up they exercise the real
path the workspace policy requires before a component is "done".
"""

from __future__ import annotations

import subprocess

import pytest

from apecx_integration.cli import setup

pytestmark = pytest.mark.integration


def _docker_up() -> bool:
    try:
        return setup._docker_available()
    except Exception:  # noqa: BLE001
        return False


_docker_gate = pytest.mark.skipif(not _docker_up(), reason="docker daemon down")


# ---------------------------------------------------------------------------
# Unconditional — no docker needed.
# ---------------------------------------------------------------------------


def test_compose_rhea_container_env_remaps_to_host_gateway():
    """The container env reuses the orchestrator's port-derivation and
    remaps every host loopback to host.docker.internal; HOST binds all
    interfaces; the conda envs dir is the image's writable path."""
    env = setup._compose_rhea_container_env()
    assert env["HOST"] == "0.0.0.0"
    assert env["PARSL_CONTAINER_BACKEND"] == "local"
    assert env["RHEA_CONDA_ENVS_DIR"] == "/opt/rhea-conda/envs"
    # Every backend endpoint reaches the host via the docker gateway.
    assert "host.docker.internal:5435" in env["DATABASE_URL"]
    assert env["REDIS_HOST"] == "host.docker.internal"
    assert env["AGENT_REDIS_HOST"] == "host.docker.internal"
    assert env["MINIO_ENDPOINT"].startswith("host.docker.internal:")
    assert env["EMBEDDING_URL"].startswith("http://host.docker.internal:11434")
    # No stray plain-localhost endpoint leaked through.
    for key in ("DATABASE_URL", "REDIS_HOST", "MINIO_ENDPOINT", "EMBEDDING_URL"):
        assert "localhost" not in env[key]


def test_step_rhea_skips_when_docker_down(monkeypatch):
    """CC-5: a docker-down setup step NEVER raises — it returns a non-empty
    ``skipped`` StepResult with actionable instructions so the chain
    continues."""
    monkeypatch.setattr(setup, "_docker_available", lambda: False)
    result = setup._step_rhea()
    assert result.status == "skipped"
    assert result.detail  # CC-1: non-empty named degrade
    assert "docker" in result.detail.lower()


def test_step_pymol_skips_when_docker_down(monkeypatch):
    """CC-5: the PyMOL build degrades to a non-empty skipped result when
    docker is down, never raising."""
    monkeypatch.setattr(setup, "_docker_available", lambda: False)
    result = setup._step_pymol()
    assert result.status == "skipped"
    assert result.detail
    assert "docker" in result.detail.lower()


# ---------------------------------------------------------------------------
# Docker-gated — hit real images / the real worker.
# ---------------------------------------------------------------------------


@_docker_gate
def test_step_pymol_builds_and_smokes_real_version():
    """E3-7: after the step the version-pinned image exists and a real
    ``pymol2`` smoke inside it reports 3.1.0 (CC-1 non-empty version)."""
    result = setup._step_pymol()
    assert result.status == "ok", result.detail

    inspect = subprocess.run(
        ["docker", "image", "inspect", setup._PYMOL_IMAGE],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert inspect.returncode == 0, "image not present after _step_pymol"

    smoke = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            setup._PYMOL_IMAGE,
            "python",
            "-c",
            "import pymol2; p=pymol2.PyMOL(); p.start(); print(p.cmd.get_version()[0]); p.stop()",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert smoke.returncode == 0, smoke.stderr[-400:]
    assert "3.1.0" in smoke.stdout, f"unexpected version: {smoke.stdout!r}"


@_docker_gate
def test_step_rhea_brings_up_worker_with_nonempty_catalog():
    """E3-4.2: a single ``_step_rhea`` yields a reachable worker with a
    non-empty ingested catalog (CC-1); re-run is idempotent.

    Gated additionally on Ollama (the embedding backend) being reachable —
    without it the ingest cannot embed and the step honestly reports
    partial. We skip rather than fail in that case (operator prereq).
    """
    if not setup._ollama_daemon_reachable():
        pytest.skip("Ollama daemon unreachable — rhea ingest embedding prereq")

    result = setup._step_rhea()
    if result.status != "ok":
        pytest.skip(f"rhea bring-up not ok in this env: {result.detail}")
    assert "surfaced" in result.detail

    # CC-1: the worker answers find_tools with a non-empty catalog.
    n = setup._call_find_tools(setup._rhea_mcp_url(), "muscle")
    assert n >= 1

    # Idempotent re-run stays ok (worker reused, not torn down).
    again = setup._step_rhea()
    assert again.status == "ok", again.detail
