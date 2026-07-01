"""Unit tests for the infra orchestrator package (2026-05-15).

What's covered
--------------

* :class:`ProbeResult` / :class:`BackendSpec` / :class:`BackendRuntime`
  contracts.
* Per-probe success + failure paths against:
  - real localhost services (the apecx-rhea-* stack on this machine)
  - a deliberately unreachable port (refused / timeout)
  - a wrong-protocol target (Redis on the Postgres port)
* Orchestrator state machine transitions:
  - ``REUSED`` when a backend is up at start_all() time.
  - ``DEGRADED`` when a previously-ready backend's probe goes red.
  - ``EXTERNAL_UNCONFIGURED`` when host-process prereq env-vars unset.
  - ``EXTERNAL_MISSING`` when an external backend's probe fails AND
    autostart is enabled.
  - ``EXTERNAL_SKIPPED`` when autostart is disabled.
* Idempotence: ``start_all`` called twice does not double-spawn.
* Spawned-vs-reused tracking: ``spawned_by_us=False`` for the live-up
  containers; atexit does NOT add them to ``_spawned_containers``.
* ``containers.py`` spec regression pins — a future image / port / env
  bump fails this test, forcing a deliberate update.

What's NOT covered here
-----------------------
The container-spawn path (`docker run`) is not unit-tested — that's
the integration test's job. Mocking subprocess to simulate `docker run`
would test the mock, not the orchestrator. The integration test
exercises the real spawn-and-poll loop.

These tests run real network probes against ``localhost`` services
on the developer machine. The probes against unreachable ports use
``localhost`` with a deliberately-wrong port number — those will
always be refused regardless of test environment.
"""

from __future__ import annotations

import os
import socket

import pytest

from apecx_integration.infrastructure import (
    APECX_REDIS,
    APECX_RHEA_MINIO,
    APECX_RHEA_POSTGRES,
    BackendSpec,
    BackendState,
    InfraOrchestrator,
    ProbeResult,
    get_orchestrator,
    reset_orchestrator_for_testing,
)
from apecx_integration.infrastructure import orchestrator as _orch_mod
from apecx_integration.infrastructure.backends import (
    BackendRuntime,
    HostProcessSpec,
    Probe,
)
from apecx_integration.infrastructure.containers import (
    all_container_specs,
    container_run_args,
)
from apecx_integration.infrastructure.orchestrator import (
    start_orchestrator_in_background_thread,
    stop_orchestrator_in_background_thread,
)
from apecx_integration.infrastructure.probes import (
    minio_probe,
    ollama_probe,
    postgres_probe,
    redis_probe,
    rhea_mcp_probe,
)


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True iff a TCP connection to ``host:port`` succeeds.

    Used by the Rhea-MCP-live-probe test to skip cleanly when Rhea
    isn't running (the test's prereq is a live host process the
    orchestrator can spawn but typically isn't running during unit
    test discovery). Mirrors the shape used by
    tests/integration/test_prewarm_workflow_live.py.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Port reachability gates for each live-probe test. Same skipif
# discipline as test_rhea_mcp_probe_against_live_localhost: a "unit"
# test that requires a non-docker-compose service to be running is
# really an integration test (friction-log #37). On a typical dev
# machine apecx-rhea-{postgres,minio,redis} + Ollama are all up; on
# CI's bare runner none of them are. Probe-then-skip lets CI run the
# unit suite cleanly and lets developers see real probe results
# locally.
_PG_LIVE = _port_open("localhost", 5435)
_REDIS_LIVE = _port_open("localhost", 6379)
_MINIO_LIVE = _port_open("localhost", 9000)
_OLLAMA_LIVE = _port_open("localhost", 11434)
_RHEA_MCP_LIVE = _port_open("localhost", 3001)

# ---------------------------------------------------------------------------
# ProbeResult / BackendSpec contracts
# ---------------------------------------------------------------------------


def test_probe_result_carries_all_fields():
    pr = ProbeResult(healthy=True, detail="ok", latency_ms=12.5)
    assert pr.healthy is True
    assert pr.detail == "ok"
    assert pr.latency_ms == 12.5
    assert pr.error is None


def test_backend_spec_rejects_kind_mismatch():
    async def _noop_probe() -> ProbeResult:
        return ProbeResult(healthy=True, detail="ok", latency_ms=0.0)

    # docker_container requires a ContainerSpec
    with pytest.raises(ValueError, match="kind='docker_container' requires"):
        BackendSpec(
            name="x",
            display_name="X",
            kind="docker_container",
            required=True,
            probe=Probe(name="x", fn=_noop_probe),
            actionable_message="-",
            container=None,
        )

    # host_process requires a HostProcessSpec
    with pytest.raises(ValueError, match="kind='host_process' requires"):
        BackendSpec(
            name="x",
            display_name="X",
            kind="host_process",
            required=True,
            probe=Probe(name="x", fn=_noop_probe),
            actionable_message="-",
            process=None,
        )

    # external forbids container / process
    with pytest.raises(ValueError, match="kind='external' must have neither"):
        BackendSpec(
            name="x",
            display_name="X",
            kind="external",
            required=True,
            probe=Probe(name="x", fn=_noop_probe),
            actionable_message="-",
            container=APECX_REDIS,
        )


def test_backend_runtime_snapshot_shape():
    async def _noop_probe() -> ProbeResult:
        return ProbeResult(healthy=True, detail="ok", latency_ms=0.0)

    spec = BackendSpec(
        name="x",
        display_name="X",
        kind="external",
        required=False,
        probe=Probe(name="x", fn=_noop_probe),
        actionable_message="-",
        tags=("a", "b"),
    )
    rt = BackendRuntime(spec=spec)
    rt.state = BackendState.READY
    rt.detail = "all good"
    rt.last_probe_at = 1234567.0
    rt.last_latency_ms = 5.5
    rt.spawned_by_us = False
    snap = rt.snapshot()
    assert snap["name"] == "x"
    assert snap["state"] == "ready"
    assert snap["detail"] == "all good"
    assert snap["latency_ms"] == 5.5
    assert snap["spawned_by_us"] is False
    assert snap["tags"] == ["a", "b"]
    assert "error" not in snap


# ---------------------------------------------------------------------------
# Container-spec regression pins
# ---------------------------------------------------------------------------


def test_postgres_container_spec_pinned():
    """A regression-pin so a deliberate image / port / env bump surfaces."""
    assert APECX_RHEA_POSTGRES.image == "pgvector/pgvector:0.8.0-pg17"
    assert APECX_RHEA_POSTGRES.container_name == "apecx-rhea-postgres"
    assert APECX_RHEA_POSTGRES.ports == ((5435, 5432),)
    env = dict(APECX_RHEA_POSTGRES.env)
    assert env["POSTGRES_PASSWORD"] == "postgres"
    assert env["POSTGRES_DB"] == "rhea"


def test_redis_container_spec_pinned():
    assert APECX_REDIS.image == "redis:7"
    assert APECX_REDIS.container_name == "apecx-redis"
    assert APECX_REDIS.ports == ((6379, 6379),)
    assert APECX_REDIS.env == ()


def test_minio_container_spec_pinned():
    assert APECX_RHEA_MINIO.image == "minio/minio"
    assert APECX_RHEA_MINIO.container_name == "apecx-rhea-minio"
    assert APECX_RHEA_MINIO.ports == ((9000, 9000), (9001, 9001))
    env = dict(APECX_RHEA_MINIO.env)
    assert env["MINIO_ROOT_USER"] == "minioadmin"
    assert env["MINIO_ROOT_PASSWORD"] == "minioadmin"
    assert APECX_RHEA_MINIO.command == ("server", "/data")


def test_all_container_specs_deterministic_order():
    specs = all_container_specs()
    names = [s.container_name for s in specs]
    assert names == ["apecx-rhea-postgres", "apecx-redis", "apecx-rhea-minio", "apecx-ollama"]


def test_container_run_args_shape():
    args = container_run_args(APECX_RHEA_MINIO)
    # leading docker run flags
    assert args[:5] == ["run", "-d", "--name", "apecx-rhea-minio"][:4] + ["-p"]
    # ports — bind to LOOPBACK by default (internal backends not world-visible)
    assert "-p" in args
    assert "127.0.0.1:9000:9000" in args
    assert "127.0.0.1:9001:9001" in args
    # env
    assert "-e" in args
    assert "MINIO_ROOT_USER=minioadmin" in args
    # command at the end
    assert args[-2:] == ["server", "/data"]
    # image just before the command
    assert args[-3] == "minio/minio"


def test_container_run_args_binds_loopback_by_default():
    """Security pin (#8, 2026-07-01): every published backend port binds 127.0.0.1, NOT
    0.0.0.0 — an unauthenticated Postgres/Redis/MinIO must not be world-visible, and a bare
    ``-p H:C`` also inserts a ufw-bypassing DNAT rule."""
    from apecx_integration.infrastructure.containers import (
        APECX_REDIS,
        APECX_RHEA_POSTGRES,
    )

    for spec in (APECX_RHEA_MINIO, APECX_RHEA_POSTGRES, APECX_REDIS):
        args = container_run_args(spec)
        pub = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
        assert pub, f"{spec.container_name} publishes no ports"
        for mapping in pub:
            assert mapping.startswith("127.0.0.1:"), (
                f"{spec.container_name} port {mapping!r} is not loopback-bound"
            )
            assert not mapping.startswith("0.0.0.0:")


def test_container_run_args_bind_host_override_exposes_world():
    """The opt-in escape hatch for an auth-fronted multi-host deploy still works."""
    args = container_run_args(APECX_REDIS, bind_host="0.0.0.0")
    pub = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
    assert pub and all(m.startswith("0.0.0.0:") for m in pub)


def test_container_run_args_emits_volume_flags_for_stateful_backends():
    """Anti-silent-failure pin: stateful backends MUST declare a
    persistent named volume so a fresh ``docker run`` doesn't silently
    create ephemeral storage that drops data on container removal."""
    pg_args = container_run_args(APECX_RHEA_POSTGRES)
    assert "-v" in pg_args
    assert "apecx-rhea-postgres-data:/var/lib/postgresql/data" in pg_args

    minio_args = container_run_args(APECX_RHEA_MINIO)
    assert "-v" in minio_args
    assert "apecx-rhea-minio-data:/data" in minio_args

    # Redis is an explicit cache — intentionally NO persistent volume.
    # If this ever changes, the spec author is forced to update both
    # this test and the docstring in containers.py.
    redis_args = container_run_args(APECX_REDIS)
    assert "-v" not in redis_args


def test_fresh_create_warning_appears_in_snapshot():
    """Anti-silent-failure pin: if the orchestrator just CREATED a
    container from scratch (vs starting a previously-stopped one), the
    warning is surfaced in the runtime's snapshot — even if the probe
    is green. A probe-green freshly-created container may have lost
    the operator's prior data, and they need to know.
    """
    from apecx_integration.infrastructure.backends import (
        BackendRuntime,
        BackendSpec,
        BackendState,
        Probe,
        ProbeResult,
    )

    async def _ok_probe() -> ProbeResult:
        return ProbeResult(healthy=True, detail="ok", latency_ms=1.0)

    spec = BackendSpec(
        name="pg_test",
        display_name="Postgres (test)",
        kind="docker_container",
        required=True,
        probe=Probe(name="pg", fn=_ok_probe),
        actionable_message="x",
        container=APECX_RHEA_POSTGRES,
    )
    rt = BackendRuntime(spec=spec, state=BackendState.READY)
    rt.fresh_create_warning = (
        "container 'apecx-rhea-postgres' was freshly created — verify your data persisted"
    )
    snap = rt.snapshot()
    assert snap["state"] == "ready"
    assert "fresh_create_warning" in snap
    assert snap["fresh_create_warning"].startswith(
        "container 'apecx-rhea-postgres' was freshly created"
    )


# ---------------------------------------------------------------------------
# Real-localhost probe success paths
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _PG_LIVE,
    reason=(
        "apecx-rhea-postgres not reachable on localhost:5435. Bring it "
        "up via `apecx-setup infra` (or any docker compose that maps "
        "5435->5432 to a postgres container) and re-run."
    ),
)
@pytest.mark.asyncio
async def test_postgres_probe_against_live_localhost():
    """The dev machine has apecx-rhea-postgres up on 5435."""
    result = await postgres_probe(
        host="localhost",
        port=5435,
        user="postgres",
        db="rhea",
        password="postgres",
    )
    assert result.healthy, f"postgres probe failed: {result}"
    assert "OK" in result.detail
    assert result.latency_ms > 0
    assert result.error is None


@pytest.mark.skipif(
    not _REDIS_LIVE,
    reason=("apecx-redis not reachable on localhost:6379. Bring it up via `apecx-setup infra`."),
)
@pytest.mark.asyncio
async def test_redis_probe_against_live_localhost():
    result = await redis_probe(host="localhost", port=6379)
    assert result.healthy, f"redis probe failed: {result}"
    assert "PONG" in result.detail
    assert result.latency_ms > 0


@pytest.mark.skipif(
    not _MINIO_LIVE,
    reason=(
        "apecx-rhea-minio not reachable on localhost:9000. Bring it up via `apecx-setup infra`."
    ),
)
@pytest.mark.asyncio
async def test_minio_probe_against_live_localhost():
    result = await minio_probe(host="localhost", port=9000)
    assert result.healthy, f"minio probe failed: {result}"
    assert "HTTP 200" in result.detail


@pytest.mark.skipif(
    not _OLLAMA_LIVE,
    reason=(
        "Ollama not reachable on localhost:11434. Install (Mac: "
        "`brew install ollama`; Linux: `curl -fsSL https://ollama.ai/"
        "install.sh | sh`) and start (`ollama serve` or `brew services "
        "start ollama`)."
    ),
)
@pytest.mark.asyncio
async def test_ollama_probe_against_live_localhost():
    result = await ollama_probe(base_url="http://localhost:11434")
    assert result.healthy, f"ollama probe failed: {result}"
    assert "model" in result.detail.lower()


@pytest.mark.skipif(
    not _RHEA_MCP_LIVE,
    reason=(
        "Rhea MCP not reachable on localhost:3001 — Rhea is a host "
        "process (not a docker container), the orchestrator spawns it "
        "on demand but it's typically NOT running during unit-test "
        "discovery. Start it via `apecx-mcp` or the orchestrator's "
        "background-thread starter, then re-run. Adding this skip "
        "fixes friction-log #N where this test failed every CI run on "
        "a machine without a live Rhea."
    ),
)
@pytest.mark.asyncio
async def test_rhea_mcp_probe_against_live_localhost():
    result = await rhea_mcp_probe(mcp_url="http://localhost:3001/mcp/")
    # rhea MCP is up; it may report >=1 tool. We only check healthy.
    assert result.healthy or "0 tools" in (result.error or "")


# ---------------------------------------------------------------------------
# Failure paths — refused / wrong protocol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_probe_handles_refused():
    """No service on port 1 — expect a clean refused result."""
    result = await postgres_probe(
        host="localhost",
        port=1,  # privileged port; nothing listening
        user="postgres",
        db="postgres",
        password="x",
        timeout_s=2.0,
    )
    assert not result.healthy
    assert result.error is not None
    assert "OperationalError" in result.error or "could not" in result.error.lower()


@pytest.mark.asyncio
async def test_redis_probe_handles_refused():
    result = await redis_probe(host="localhost", port=1, timeout_s=2.0)
    assert not result.healthy
    assert result.error is not None


@pytest.mark.asyncio
async def test_minio_probe_handles_refused():
    result = await minio_probe(host="localhost", port=1, timeout_s=2.0)
    assert not result.healthy
    assert result.error is not None


@pytest.mark.asyncio
async def test_ollama_probe_handles_unreachable():
    result = await ollama_probe(base_url="http://localhost:1", timeout_s=2.0)
    assert not result.healthy
    assert result.error is not None


@pytest.mark.asyncio
async def test_rhea_mcp_probe_handles_unreachable():
    result = await rhea_mcp_probe(mcp_url="http://localhost:1/mcp/", timeout_s=2.0)
    assert not result.healthy
    assert result.error is not None


@pytest.mark.asyncio
async def test_redis_probe_on_postgres_port_is_wrong_protocol():
    """Hit Redis client at the Postgres port — wrong protocol path."""
    # Port 5435 is the live pgvector container. The redis client will
    # either fail to handshake or time out reading PONG.
    result = await redis_probe(host="localhost", port=5435, timeout_s=2.0)
    assert not result.healthy
    assert result.error is not None


@pytest.mark.asyncio
async def test_postgres_probe_on_redis_port_is_wrong_protocol():
    """Hit Postgres client at the Redis port — wrong protocol path."""
    result = await postgres_probe(
        host="localhost",
        port=6379,
        user="postgres",
        db="postgres",
        password="x",
        timeout_s=2.0,
    )
    assert not result.healthy
    assert result.error is not None


# ---------------------------------------------------------------------------
# Orchestrator state-machine
# ---------------------------------------------------------------------------


def _build_orchestrator_with_one_probe(
    probe_returns: list[ProbeResult],
    *,
    kind: str = "external",
    required: bool = True,
    process_prereqs: tuple[str, ...] = (),
) -> tuple[InfraOrchestrator, list[int]]:
    """Build an orchestrator with a single fake backend whose probe
    returns the queued results in order. ``probe_call_count`` is
    returned so the test can assert the orchestrator only re-probed
    the expected number of times.
    """
    call_count = [0]

    async def _fake_probe() -> ProbeResult:
        idx = call_count[0]
        call_count[0] += 1
        # Repeat the last result if we run out.
        return probe_returns[min(idx, len(probe_returns) - 1)]

    probe = Probe(name="fake", fn=_fake_probe)

    if kind == "external":
        spec = BackendSpec(
            name="fake",
            display_name="Fake",
            kind="external",
            required=required,
            probe=probe,
            actionable_message="install + start fake",
        )
    elif kind == "host_process":
        # A host_process with prereq vars; command_factory is never
        # called in these tests (we never reach the spawn path).
        def _never_called(env):
            raise AssertionError("command_factory should not run")

        process_spec = HostProcessSpec(
            prereq_env_vars=process_prereqs,
            command_factory=_never_called,
            ready_timeout_s=1.0,
        )
        spec = BackendSpec(
            name="fake",
            display_name="Fake host proc",
            kind="host_process",
            required=required,
            probe=probe,
            actionable_message="set prereqs",
            process=process_spec,
        )
    else:
        raise ValueError(f"unsupported kind for this helper: {kind}")

    orch = InfraOrchestrator(
        specs=[spec],
        autostart_enabled=True,
        docker_binary=None,  # never used in these paths
    )
    return orch, call_count


@pytest.mark.asyncio
async def test_orchestrator_marks_backend_reused_when_first_probe_healthy():
    orch, calls = _build_orchestrator_with_one_probe(
        [ProbeResult(healthy=True, detail="ok", latency_ms=1.0)],
    )
    snap = await orch.start_all()
    backend = snap["backends"][0]
    assert backend["state"] == "reused"
    assert backend["spawned_by_us"] is False
    # start_all probes once; status() then re-probes once.
    assert calls[0] >= 1


@pytest.mark.asyncio
async def test_orchestrator_marks_external_missing_when_probe_fails():
    orch, _ = _build_orchestrator_with_one_probe(
        [ProbeResult(healthy=False, detail="refused", latency_ms=1.0, error="econnrefused")],
    )
    snap = await orch.start_all()
    backend = snap["backends"][0]
    assert backend["state"] == "external_missing"
    assert "install + start fake" in backend["detail"]


@pytest.mark.asyncio
async def test_orchestrator_marks_external_skipped_when_autostart_off():
    async def _failing() -> ProbeResult:
        return ProbeResult(healthy=False, detail="refused", latency_ms=1.0, error="x")

    spec = BackendSpec(
        name="fake",
        display_name="Fake",
        kind="external",
        required=True,
        probe=Probe(name="fake", fn=_failing),
        actionable_message="-",
    )
    orch = InfraOrchestrator(
        specs=[spec],
        autostart_enabled=False,
        docker_binary=None,
    )
    snap = await orch.start_all()
    backend = snap["backends"][0]
    assert backend["state"] == "external_skipped"


@pytest.mark.asyncio
async def test_orchestrator_marks_host_process_unconfigured_when_prereqs_missing():
    # Use a prereq var that definitely isn't set.
    bogus_var = "NO_SUCH_VAR_ZZZ_2026_05_15"
    os.environ.pop(bogus_var, None)

    orch, _ = _build_orchestrator_with_one_probe(
        [ProbeResult(healthy=False, detail="refused", latency_ms=1.0, error="x")],
        kind="host_process",
        process_prereqs=(bogus_var,),
    )
    snap = await orch.start_all()
    backend = snap["backends"][0]
    assert backend["state"] == "external_unconfigured"
    assert bogus_var in backend["detail"]


@pytest.mark.asyncio
async def test_orchestrator_degrades_ready_backend_on_status_reprobe_failure():
    # First probe (during start_all): healthy → REUSED.
    # Second probe (during status): unhealthy → DEGRADED.
    orch, _ = _build_orchestrator_with_one_probe(
        [
            ProbeResult(healthy=True, detail="ok", latency_ms=1.0),
            ProbeResult(healthy=False, detail="gone", latency_ms=1.0, error="lost"),
        ],
    )
    await orch.start_all()
    snap = await orch.status()
    backend = snap["backends"][0]
    assert backend["state"] == "degraded"
    assert backend["error"] == "lost"


@pytest.mark.asyncio
async def test_orchestrator_recovers_from_degraded_when_probe_flips_green():
    # ``start_all`` itself calls status() at the end, so it consumes
    # TWO probe results (initial probe + status re-probe). The
    # sequence below is therefore: probe-1 → REUSED, probe-2 →
    # DEGRADED (this is what we observe right after start_all),
    # probe-3 → READY/REUSED (the next status call sees recovery).
    orch, _ = _build_orchestrator_with_one_probe(
        [
            ProbeResult(healthy=True, detail="ok", latency_ms=1.0),
            ProbeResult(healthy=False, detail="gone", latency_ms=1.0, error="lost"),
            ProbeResult(healthy=True, detail="back", latency_ms=1.0),
        ],
    )
    snap_start = await orch.start_all()
    assert snap_start["backends"][0]["state"] == "degraded"
    snap2 = await orch.status()
    # Was REUSED before — recovery keeps it REUSED (we didn't spawn).
    assert snap2["backends"][0]["state"] == "reused"


@pytest.mark.asyncio
async def test_orchestrator_start_all_is_idempotent():
    """Calling start_all twice does not double-process backends."""
    orch, calls = _build_orchestrator_with_one_probe(
        [ProbeResult(healthy=True, detail="ok", latency_ms=1.0)],
    )
    snap1 = await orch.start_all()
    calls_after_first = calls[0]
    snap2 = await orch.start_all()
    # Second start_all re-probes (1 call per backend per start_all +
    # 1 status re-probe) — we just assert it doesn't transition us
    # away from REUSED.
    assert snap1["backends"][0]["state"] == "reused"
    assert snap2["backends"][0]["state"] == "reused"
    # No spawn happened, no spawned-container / spawned-process tracking.
    assert snap2["backends"][0]["spawned_by_us"] is False
    # The probe was called at least once more during the second start_all.
    assert calls[0] > calls_after_first


@pytest.mark.asyncio
async def test_orchestrator_actionable_messages_populated_on_failure():
    orch, _ = _build_orchestrator_with_one_probe(
        [ProbeResult(healthy=False, detail="refused", latency_ms=1.0, error="x")],
    )
    snap = await orch.start_all()
    assert len(snap["actionable"]) >= 1
    assert "fake" in snap["actionable"][0].lower() or "fake" in snap["actionable"][0]


@pytest.mark.asyncio
async def test_orchestrator_overall_state_aggregation():
    """Overall state derives from per-backend states."""

    # Two backends: one ready, one external_missing — overall == degraded.
    async def _ok() -> ProbeResult:
        return ProbeResult(healthy=True, detail="ok", latency_ms=1.0)

    async def _bad() -> ProbeResult:
        return ProbeResult(healthy=False, detail="refused", latency_ms=1.0, error="x")

    specs = [
        BackendSpec(
            name="a",
            display_name="A",
            kind="external",
            required=True,
            probe=Probe(name="a", fn=_ok),
            actionable_message="-",
        ),
        BackendSpec(
            name="b",
            display_name="B",
            kind="external",
            required=True,
            probe=Probe(name="b", fn=_bad),
            actionable_message="install B",
        ),
    ]
    orch = InfraOrchestrator(specs=specs, autostart_enabled=True, docker_binary=None)
    snap = await orch.start_all()
    assert snap["overall"] == "degraded"

    # Now: optional missing → still overall=ready.
    specs[1] = BackendSpec(
        name="b",
        display_name="B",
        kind="external",
        required=False,
        probe=Probe(name="b", fn=_bad),
        actionable_message="install B",
    )
    orch2 = InfraOrchestrator(specs=specs, autostart_enabled=True, docker_binary=None)
    snap2 = await orch2.start_all()
    assert snap2["overall"] == "ready"


@pytest.mark.asyncio
async def test_orchestrator_does_not_track_reused_containers_for_atexit():
    """A container that's already up is REUSED — we never registered
    it in ``_spawned_containers``, so atexit will not stop it.

    This is the most important honesty contract of the orchestrator.
    """
    orch, _ = _build_orchestrator_with_one_probe(
        [ProbeResult(healthy=True, detail="ok", latency_ms=1.0)],
    )
    await orch.start_all()
    assert orch._spawned_containers == []
    assert orch._spawned_processes == []


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def test_get_orchestrator_returns_singleton():
    reset_orchestrator_for_testing()
    a = get_orchestrator()
    b = get_orchestrator()
    assert a is b
    reset_orchestrator_for_testing()


def test_reset_orchestrator_clears_singleton():
    reset_orchestrator_for_testing()
    a = get_orchestrator()
    reset_orchestrator_for_testing()
    b = get_orchestrator()
    assert a is not b
    reset_orchestrator_for_testing()


# ---------------------------------------------------------------------------
# Background-drive thread lifecycle (Fix B) + probe-only prewarm gate (Fix E)
# ---------------------------------------------------------------------------
class _FakeDrivenOrch:
    """Minimal stand-in whose start_all/prewarm are instant + observable."""

    def __init__(self, autostart: bool) -> None:
        self._autostart = autostart
        self.start_all_called = False
        self.prewarm_called = False

    async def start_all(self):
        self.start_all_called = True
        return {}

    async def prewarm_workflow_tools(self):
        self.prewarm_called = True


def test_stop_background_thread_is_noop_when_none_running():
    # Safe + idempotent even if nothing was ever started, or a prior
    # thread already finished. Must not raise.
    stop_orchestrator_in_background_thread(timeout=1.0)
    stop_orchestrator_in_background_thread(timeout=1.0)


def test_background_drive_runs_prewarm_in_full_mode(monkeypatch):
    fake = _FakeDrivenOrch(autostart=True)
    monkeypatch.setattr(_orch_mod, "get_orchestrator", lambda: fake)
    thread = start_orchestrator_in_background_thread()
    # stop() joins; the join is the synchronization point — after it
    # returns the drive has finished and the flags are settled.
    stop_orchestrator_in_background_thread(timeout=10.0)
    assert not thread.is_alive()
    assert fake.start_all_called is True
    assert fake.prewarm_called is True


def test_background_drive_skips_prewarm_in_probe_only_mode(monkeypatch):
    fake = _FakeDrivenOrch(autostart=False)
    monkeypatch.setattr(_orch_mod, "get_orchestrator", lambda: fake)
    thread = start_orchestrator_in_background_thread()
    stop_orchestrator_in_background_thread(timeout=10.0)
    assert not thread.is_alive()
    assert fake.start_all_called is True
    # Probe-only must NOT build conda envs.
    assert fake.prewarm_called is False
