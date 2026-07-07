"""Dataclasses + state enum that describe a managed backend.

The orchestrator owns a list of :class:`BackendSpec` values. Each
spec is one of two kinds:

* ``docker_container`` — a long-lived container with a well-defined
  health probe (the container is reusable across MCP-server starts).
  A container with a non-``"no"`` ``restart`` policy is Docker-lifecycle-
  owned: the daemon auto-restarts it across reboots and the orchestrator
  does NOT teardown-track it.
* ``external`` — entirely operator-managed (Ollama). The orchestrator
  probes only.

The probe contract is uniform: each backend carries a
:class:`Probe` whose ``probe()`` returns a :class:`ProbeResult` with
``healthy: bool``, a short detail string, latency in ms, and an
optional error message. Probes MUST NOT raise on a normal failure
(e.g. connection-refused) — they must catch and return ``healthy=False``
with an actionable detail. They MAY raise on programmer error (wrong
type, missing required attribute).

State machine
-------------
::

    missing ──start──► starting ──probe-ok──► ready ──probe-fail──► degraded
                                                                    │
                                                       ┌────────────┘
                                                       ▼
                                                     down
    external_skipped (operator opted-out)
    external_missing (probe fails AND we can't spawn)
    external_unconfigured (probe fails AND prereq env-vars unset)
    reused (was already up at start_all() time)
    error_starting (autostart failed; carries detail)

Pydantic is intentionally NOT used here. These dataclasses are
plumbing — they ship over an in-process boundary, never get
serialized to YAML, never round-trip through the framework's
``from_config`` pipeline.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class BackendState(StrEnum):
    """The state machine values reported via :meth:`InfraOrchestrator.status`.

    String-valued so it serializes naturally into the JSON the MCP
    ``infrastructure_status`` tool returns.
    """

    # Pre-startup
    MISSING = "missing"
    # In-flight transitions
    STARTING = "starting"
    # Terminal success
    READY = "ready"
    REUSED = "reused"  # was up when we got here, we didn't spawn it
    # Terminal failure (transient — re-probing may flip back)
    DEGRADED = "degraded"
    DOWN = "down"
    # Operator-prereq paths
    EXTERNAL_SKIPPED = "external_skipped"  # APECX_MCP_AUTOSTART_INFRA=0 or required=False
    EXTERNAL_MISSING = "external_missing"  # operator must install (Ollama)
    EXTERNAL_UNCONFIGURED = "external_unconfigured"  # prereq env-vars unset
    ERROR_STARTING = "error_starting"  # autostart attempted + failed


@dataclass(frozen=True)
class ProbeResult:
    """The output of a backend health probe.

    ``healthy`` and ``detail`` are required. ``latency_ms`` measures
    the probe RTT. ``error`` is set on failure for the operator's
    diagnostic message (never empty when ``healthy=False``).
    """

    healthy: bool
    detail: str
    latency_ms: float
    error: str | None = None
    # Whether the backend responded at all (vs. connection-refused / timeout). A backend can be
    # ``reachable=True, healthy=False`` — up but not fully provisioned (e.g. an Ollama container that
    # is serving but has not pulled its model yet). Bring-up uses this to mark such a spawn DEGRADED
    # (up, needs provisioning) rather than ERROR_STARTING (failed to come up). Defaults True; a probe
    # sets it False only when the endpoint could not be contacted. (#7)
    reachable: bool = True


# Async function signature shared by every probe.
ProbeCallable = Callable[[], Awaitable[ProbeResult]]


@dataclass(frozen=True)
class Probe:
    """A named async health probe."""

    name: str
    fn: ProbeCallable

    async def run(self) -> ProbeResult:
        return await self.fn()


@dataclass(frozen=True)
class ContainerSpec:
    """A Docker container specification.

    The orchestrator uses ``docker run -d --name <container_name>``
    via subprocess to bring this up when the probe reports the
    backend is down. ``ready_timeout_s`` bounds how long we'll poll
    the probe after spawning before declaring failure.

    Volumes and labels are optional; the running rhea-postgres /
    rhea-minio containers use named volumes the operator owns, so
    we never declare a volume here — we just reuse whatever they
    already have. If the container has been destroyed entirely we
    re-create it without the volume (data loss is the operator's
    surprise, not ours; we log it loudly).
    """

    image: str
    container_name: str
    # Ordered list of ``-p HOST:CONTAINER`` entries.
    ports: tuple[tuple[int, int], ...]
    # Ordered list of ``-e KEY=VALUE`` entries.
    env: tuple[tuple[str, str], ...] = ()
    # Named volumes / bind mounts to attach. Each entry is
    # (source, container_path). A bare name like ``apecx-rhea-postgres-data``
    # becomes a Docker named volume — it survives `docker rm` and is the
    # right shape for stateful services (Postgres, MinIO). Without
    # volumes a fresh ``docker run`` writes data into the container's
    # ephemeral layer; that data is silently lost when the container is
    # removed. The orchestrator surfaces a fresh-create warning in
    # ``actionable`` when it takes the ``docker run`` (not ``docker
    # start``) path on a volume-bearing container, so the operator knows
    # the named volume may or may not still hold prior state.
    volumes: tuple[tuple[str, str], ...] = ()
    # Extra ``docker run`` flags inserted BEFORE the image (after name/ports/
    # env/volumes). For flags that have no dedicated field — e.g.
    # ``--add-host=host.docker.internal:host-gateway`` so a container started by
    # the orchestrator can reach the host-published infra ports (rhea-server
    # reaching postgres/redis/minio/ollama). Each entry is one token, e.g.
    # ``("--add-host=host.docker.internal:host-gateway",)``.
    extra_run_args: tuple[str, ...] = ()
    # Ordered list of extra positional args appended after the image.
    # E.g. ``("server", "/data")`` for minio.
    command: tuple[str, ...] = ()
    # How long to wait after spawning for the probe to flip green.
    ready_timeout_s: float = 30.0
    # Docker ``--restart`` policy. A non-``"no"`` policy (e.g.
    # ``"unless-stopped"``) makes the Docker daemon auto-restart the container
    # on host reboot — the stack survives an OS restart WITHOUT anything
    # relaunching apecx-mcp. It ALSO marks the container LONG-LIVED: the
    # orchestrator must NOT ``docker stop`` it on apecx-mcp exit (a stop cancels
    # the restart policy), so a restart-policy container is never enrolled in
    # ``_spawned_containers`` for atexit teardown — Docker owns its lifecycle.
    # ``"no"`` (the default) keeps the ephemeral/teardown-on-exit semantics.
    restart: str = "no"


BackendKind = Literal["docker_container", "external"]


@dataclass(frozen=True)
class BackendSpec:
    """A complete description of one managed backend.

    ``container`` is populated for a ``docker_container`` kind and left
    None for ``external``. ``probe`` is mandatory for every kind — even
    "external" backends need a probe so the orchestrator can report
    their state.

    ``actionable_message`` is shown to the operator when the backend
    is in a non-ready terminal state (DEGRADED / DOWN /
    EXTERNAL_MISSING / EXTERNAL_UNCONFIGURED / ERROR_STARTING). It MUST
    tell them exactly what to do to fix it (an install link, an env
    var to set, etc.). FAIL-LOUD-with-remedy is the contract.
    """

    name: str
    display_name: str
    kind: BackendKind
    required: bool
    probe: Probe
    actionable_message: str
    container: ContainerSpec | None = None
    # Free-form descriptive tags that surface in the status tool.
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "docker_container" and self.container is None:
            raise ValueError(
                f"BackendSpec {self.name!r}: kind='docker_container' requires "
                f"a ContainerSpec; got None"
            )
        if self.kind == "external" and self.container is not None:
            raise ValueError(
                f"BackendSpec {self.name!r}: kind='external' must have no "
                f"container set; the operator manages it entirely."
            )


@dataclass
class BackendRuntime:
    """Mutable per-backend runtime state held by the orchestrator.

    The orchestrator owns one of these per :class:`BackendSpec`. The
    status tool reads ``snapshot()`` to build its return payload.
    """

    spec: BackendSpec
    state: BackendState = BackendState.MISSING
    detail: str = ""
    last_probe_at: float = 0.0
    last_latency_ms: float = 0.0
    error: str | None = None
    spawned_by_us: bool = False
    # Populated for docker backends only; identifies the container we
    # spawned (for atexit cleanup). When we reuse an existing container
    # this stays None.
    spawned_container: str | None = None
    # When the orchestrator created a container from scratch (``docker
    # run``, not ``docker start`` on a pre-existing stopped container),
    # this carries an operator-actionable warning. The status tool
    # surfaces it. The point: if the operator had data in a prior
    # container that they've since removed, we just created a clean
    # replacement and they should know — ``ready`` from a probe does
    # NOT prove their data survived.
    fresh_create_warning: str | None = None
    # Whether the LAST probe could contact the endpoint at all (vs connection-refused/timeout). Lets
    # the dashboard monitor distinguish "up but degraded" (reachable — e.g. Ollama with no model, do
    # NOT restart) from "genuinely down" (unreachable — e.g. a stopped container, restart). (#7 / W3)
    reachable: bool = True

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe dict for the status tool."""
        out: dict[str, Any] = {
            "name": self.spec.name,
            "display_name": self.spec.display_name,
            "kind": self.spec.kind,
            "required": self.spec.required,
            "state": self.state.value,
            "reachable": self.reachable,
            "detail": self.detail,
            "last_probe_at": self.last_probe_at,
            "latency_ms": self.last_latency_ms,
            "spawned_by_us": self.spawned_by_us,
            "tags": list(self.spec.tags),
        }
        if self.error:
            out["error"] = self.error
        if self.fresh_create_warning:
            out["fresh_create_warning"] = self.fresh_create_warning
        if self.spawned_container:
            out["spawned_container"] = self.spawned_container
        return out


__all__ = [
    "BackendKind",
    "BackendRuntime",
    "BackendSpec",
    "BackendState",
    "ContainerSpec",
    "Probe",
    "ProbeCallable",
    "ProbeResult",
]
