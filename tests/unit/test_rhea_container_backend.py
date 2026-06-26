"""Phase-2 rhea CONTAINER backend: deterministic spec/argv composition.

The rhea-server can run as a host PROCESS (default, uses the host conda) or as a
Docker CONTAINER (host-conda-independent). These tests pin the container
backend's generated ``docker run`` to the command that was verified end-to-end
in the containerization spike (a real MUSCLE alignment through the containerized
server, host conda broken):

    docker run -d --name <name> -p 3001:3001 \
      -e ...host.docker.internal endpoints... \
      -e PARSL_CONTAINER_BACKEND=local -e AGENT_HANDLE_TIMEOUT=900 \
      --add-host=host.docker.internal:host-gateway <image>

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
    """Unset APECX_RHEA_BACKEND -> the CONTAINER backend (default, host-conda-independent)."""
    rhea = _rhea_spec(monkeypatch, None)
    assert rhea.kind == "docker_container"
    assert rhea.container is not None
    assert rhea.process is None
    assert rhea.container.ports == ((3001, 3001),)


def test_host_backend_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """APECX_RHEA_BACKEND=host -> the host-process backend (opt-out of the container default)."""
    rhea = _rhea_spec(monkeypatch, "host")
    assert rhea.kind == "host_process"
    assert rhea.process is not None
    assert rhea.container is None


def test_container_backend_selected_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    rhea = _rhea_spec(monkeypatch, "container")
    assert rhea.kind == "docker_container"
    assert rhea.container is not None
    assert rhea.process is None
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
    # Parsl LOCAL backend -> worker is a subprocess INSIDE the container (uses
    # the container's conda; shares the netns, no interchange problem).
    assert env["PARSL_CONTAINER_BACKEND"] == "local"
    # Generous handle timeout: first tool call cold-builds the conda env and the
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
    assert "-p" in argv and "3001:3001" in argv
    # add-host must appear BEFORE the image (it is a run flag, not a CMD arg).
    assert "--add-host=host.docker.internal:host-gateway" in argv
    assert argv.index("--add-host=host.docker.internal:host-gateway") < argv.index(
        rhea.container.image
    )
    # Spot-check the load-bearing env made it onto the command line.
    assert "PARSL_CONTAINER_BACKEND=local" in joined
    assert "host.docker.internal" in joined
    assert "localhost" not in joined


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
