"""rhea CONTAINER backend: deterministic spec/argv composition.

The rhea-server runs ONLY as a Docker CONTAINER (host-conda-independent) — the
host-process backend and the ``APECX_RHEA_BACKEND`` switch were deleted in the
rhea-container single-path refactor. These tests pin the container
backend's generated ``docker run`` to the command that was verified end-to-end
in the containerization spike (a real MUSCLE alignment through the containerized
server, host conda broken):

    docker run -d --name <name> -p 127.0.0.1:3001:3001 \
      -e ...host.docker.internal endpoints... \
      -e PARSL_CONTAINER_BACKEND=docker -e TMPDIR=/tmp/apecx-rhea-tmp \
      -e AGENT_HANDLE_TIMEOUT=900 \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v /tmp/apecx-rhea-tmp:/tmp/apecx-rhea-tmp \
      --add-host host.docker.internal:host-gateway <image>

Per-tool container execution (rhea P2b): the socket mount lets the in-container
direct agent `docker run` each tool's biocontainer on the host daemon, and the
shared work dir (same abs path both sides) makes a tool's relative output resolve.

No real Docker/MinIO/Redis is touched — these exercise the PURE spec composition
(the legit unit-test carve-out). The orchestrator-driven live bring-up is the
matching integration test.

The load-bearing assertion is the SILENT-FAILURE guard: NO env value may contain
``localhost``/``127.0.0.1``. A leak there means the container would dial its own
loopback instead of the host-published infra port — the probe would still go
green (the MCP server answers) while every tool call fails. That is exactly the
"green tests, broken product" shape this backend must not reintroduce.
"""

from __future__ import annotations

import pytest

from apecx_integration.infrastructure.backends import ContainerSpec
from apecx_integration.infrastructure.containers import container_run_args
from apecx_integration.infrastructure.orchestrator import _default_backend_specs


@pytest.fixture(autouse=True)
def _isolate_rhea_env(monkeypatch: pytest.MonkeyPatch):
    """Isolate from ambient/leaked rhea env vars. _compose_rhea_container_env reads os.environ
    (e.g. RHEA_CONDA_ENVS_DIR copies through, AGENT_HANDLE_TIMEOUT overrides the default), so a
    sibling test (e.g. test_rhea_env_autodiscovery) that sets them must not pollute these
    spec-composition assertions. Clearing them per-test makes this file order-independent."""
    for _var in ("RHEA_CONDA_ENVS_DIR", "AGENT_HANDLE_TIMEOUT", "PARSL_CONTAINER_BACKEND"):
        monkeypatch.delenv(_var, raising=False)


def _rhea_spec(monkeypatch: pytest.MonkeyPatch, backend: str | None):
    if backend is None:
        monkeypatch.delenv("APECX_RHEA_BACKEND", raising=False)
    else:
        monkeypatch.setenv("APECX_RHEA_BACKEND", backend)
    return {s.name: s for s in _default_backend_specs()}["rhea_mcp"]


def test_default_backend_is_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rhea is ALWAYS the CONTAINER backend (single path, host-conda-independent).

    The host-process backend and the ``APECX_RHEA_BACKEND`` switch were deleted
    in the rhea-container single-path refactor — there is no other kind to select.
    """
    rhea = _rhea_spec(monkeypatch, None)
    assert rhea.kind == "docker_container"
    assert rhea.container is not None
    assert rhea.container.ports == ((3001, 3001),)


def test_rhea_backend_env_is_ignored_switch_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-path invariant: setting APECX_RHEA_BACKEND=host still yields a container.

    The env switch that used to opt into a host-process backend was removed; proving
    it is inert guards against a silent reintroduction of the two-path fork.

    Integration parity: the real orchestrator-driven container bring-up is covered by
    tests/integration/test_rhea_container_backend_live.py (orchestrator spawns the
    real rhea-server image).
    """
    rhea = _rhea_spec(monkeypatch, "host")
    assert rhea.kind == "docker_container"
    assert rhea.container is not None
    assert rhea.container.ports == ((3001, 3001),)


def test_container_env_has_no_localhost_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """SILENT-FAILURE GUARD: a container cannot reach infra via localhost.

    Every infra endpoint must be host.docker.internal; any localhost/127.0.0.1
    value would make tool calls fail while the probe stays green.
    """
    rhea = _rhea_spec(monkeypatch, "container")
    env = dict(rhea.container.env)
    leaks = {k: v for k, v in env.items() if "localhost" in v or "127.0.0.1" in v}
    assert not leaks, f"localhost/127.0.0.1 leaked into container env: {leaks}"
    # Positively: the infra endpoints resolve to the host alias.
    assert "host.docker.internal" in env["DATABASE_URL"]
    assert env["REDIS_HOST"] == "host.docker.internal"
    assert env["AGENT_REDIS_HOST"] == "host.docker.internal"
    assert env["MINIO_ENDPOINT"].startswith("host.docker.internal:")
    assert "host.docker.internal" in env["EMBEDDING_URL"]


def test_container_env_container_specific_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rhea = _rhea_spec(monkeypatch, "container")
    env = dict(rhea.container.env)
    # Per-tool container execution (P2b): the tool ENGINE is docker. In direct mode
    # `parsl_container_backend` is ONLY the tool engine (the parsl worker launcher is gated on the
    # HPC provider, never invoked here), so "docker" does not reintroduce the macOS worker problem.
    assert env["PARSL_CONTAINER_BACKEND"] == "docker"
    # The tool work dir is the host-shared mount so a tool's relative output resolves both sides.
    assert env["TMPDIR"] == "/tmp/apecx-rhea-tmp"
    # Generous handle timeout: first tool call cold-pulls + runs the biocontainer and the
    # handle is written only after; the 30s default would time out.
    assert int(env["AGENT_HANDLE_TIMEOUT"]) >= 300
    # Binds all interfaces so -p 3001:3001 is reachable from the host.
    assert env["HOST"] == "0.0.0.0"
    # The host-cache conda dir does NOT exist inside the container; let the
    # image's baked RHEA_CONDA_ENVS_DIR (/opt/rhea-conda/envs) win.
    assert "RHEA_CONDA_ENVS_DIR" not in env


def test_rhea_serve_port_follows_rhea_mcp_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawned Rhea server's PORT follows $RHEA_MCP_URL (which apecx-mcp derives from the
    config's rhea.host/port), so a non-default rhea port keeps the server + the probe in sync."""
    monkeypatch.setenv("RHEA_MCP_URL", "http://localhost:3009/mcp/")
    env = dict(_rhea_spec(monkeypatch, "container").container.env)
    assert env["PORT"] == "3009"


def test_rhea_serve_port_defaults_to_3001(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no $RHEA_MCP_URL the spawned server keeps the 3001 default (common path unchanged)."""
    monkeypatch.delenv("RHEA_MCP_URL", raising=False)
    env = dict(_rhea_spec(monkeypatch, "container").container.env)
    assert env["PORT"] == "3001"


def test_container_run_args_matches_verified_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generated argv == the spike-proven working command (modulo name/tag)."""
    rhea = _rhea_spec(monkeypatch, "container")
    argv = container_run_args(rhea.container)
    joined = " ".join(argv)
    assert argv[:2] == ["run", "-d"]
    # Host port binds LOOPBACK (#8) — Rhea is an internal worker, not world-visible. The
    # container still binds 0.0.0.0 INTERNALLY (env HOST above) so the docker port map works.
    assert "-p" in argv and "127.0.0.1:3001:3001" in argv
    # add-host must appear BEFORE the image (it is a run flag, not a CMD arg). It is
    # emitted as two tokens (`--add-host <val>`) from the dedicated `extra_hosts` field.
    assert "--add-host" in argv
    ah_i = argv.index("--add-host")
    assert argv[ah_i + 1] == "host.docker.internal:host-gateway"
    assert ah_i < argv.index(rhea.container.image)
    # Spot-check the load-bearing env made it onto the command line.
    assert "PARSL_CONTAINER_BACKEND=docker" in joined
    assert "host.docker.internal" in joined
    assert "localhost" not in joined
    # Per-tool container execution (P2b): the docker socket + shared work dir are mounted so the
    # in-container agent can `docker run` each tool's biocontainer and its relative output resolves.
    assert "-v" in argv
    assert "/var/run/docker.sock:/var/run/docker.sock" in argv
    assert "/tmp/apecx-rhea-tmp:/tmp/apecx-rhea-tmp" in argv


def test_rhea_container_spec_mounts_docker_socket_and_shared_tmp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2a: the rhea ContainerSpec bind-mounts the docker socket + a shared tool work dir.

    Without the socket mount the in-container direct agent's `docker run <biocontainer>` has no daemon
    to reach (every tool run fails at `docker pull`); without the shared work dir at the SAME abs path
    both sides, a tool's relative output is written where the server can't read it. Pins both mounts
    independently of the argv spot-check so a regression naming only one is still caught.
    """
    rhea = _rhea_spec(monkeypatch, "container")
    volumes = dict(rhea.container.volumes)
    assert volumes.get("/var/run/docker.sock") == "/var/run/docker.sock"
    assert volumes.get("/tmp/apecx-rhea-tmp") == "/tmp/apecx-rhea-tmp"


def test_building_rhea_spec_has_no_filesystem_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec composition is PURE — building the rhea spec must NOT touch the host filesystem.

    The shared tool work dir is created at BRING-UP time (just before `docker run`), not at
    spec-build time — `_default_backend_specs` runs on every roster build (reconcile/status/tests),
    so a `makedirs` there would be an unconditional `/tmp` write during pure composition. Fail loud
    if any spec-build path calls `os.makedirs`.
    """

    def _boom(*_a, **_k):
        raise AssertionError("spec build must not touch the filesystem (os.makedirs)")

    monkeypatch.setattr("apecx_integration.infrastructure.orchestrator.os.makedirs", _boom)
    # Must not raise — spec composition is side-effect-free.
    _rhea_spec(monkeypatch, "container")


def test_rhea_container_spec_has_restart_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rhea container is Docker-lifecycle-owned: restart=unless-stopped so it
    survives an OS reboot without anything relaunching apecx-mcp, and it carries the
    host-gateway add-host so it can reach the host-published infra ports.

    Integration parity: tests/integration/test_rhea_container_backend_live.py brings
    the real container up through the orchestrator and confirms the restart policy is
    applied by the Docker daemon.
    """
    rhea = _rhea_spec(monkeypatch, None)
    assert rhea.container.restart == "unless-stopped"
    assert rhea.container.extra_hosts == ("host.docker.internal:host-gateway",)
    # Autodeploy (P4-B): the spec carries the async build hook so the orchestrator
    # auto-builds the image from local rhea source before `docker run` — no
    # `apecx-setup rhea` build step.
    from apecx_integration.infrastructure.rhea_server_provisioner import (
        ensure_rhea_image_built,
    )

    assert rhea.container.image_builder is ensure_rhea_image_built


def test_container_run_args_emits_restart_for_rhea(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--restart <policy>`` is emitted right after ``-d`` for a restart-policy spec."""
    rhea = _rhea_spec(monkeypatch, None)
    argv = container_run_args(rhea.container)
    assert argv[:4] == ["run", "-d", "--restart", "unless-stopped"]


def test_container_run_args_omits_restart_when_policy_is_no() -> None:
    """A default (restart="no") spec emits NO ``--restart`` flag."""
    spec = ContainerSpec(
        image="img:tag",
        container_name="c",
        ports=((1, 2),),
    )
    argv = container_run_args(spec)
    assert "--restart" not in argv
    assert argv[:2] == ["run", "-d"]
    # -d is immediately followed by --name (no restart flag between).
    assert argv[2] == "--name"


def test_extra_run_args_inserted_before_image() -> None:
    """container_run_args places extra_run_args after -e/-v and before the image."""
    spec = ContainerSpec(
        image="img:tag",
        container_name="c",
        ports=((1, 2),),
        env=(("K", "V"),),
        extra_run_args=("--add-host=h:host-gateway", "--cap-add=SYS_PTRACE"),
        command=("serve",),
    )
    argv = container_run_args(spec)
    img_i = argv.index("img:tag")
    for flag in ("--add-host=h:host-gateway", "--cap-add=SYS_PTRACE"):
        assert argv.index(flag) < img_i
    # command still trails the image.
    assert argv[-1] == "serve"
    assert argv.index("serve") > img_i
