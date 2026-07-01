"""Health probes for the five managed backends.

Each probe is an ``async`` function returning :class:`ProbeResult`.
Probes follow a uniform shape:

* Catch the protocol-level connection errors (refused, timeout,
  wrong protocol) and return ``healthy=False`` with an actionable
  detail. Probes MUST NOT raise on those — the orchestrator runs
  them in parallel and a raise would taint sibling backends'
  reporting.
* On success, set ``detail`` to a short status string (e.g.
  ``"5 model(s) loaded"`` for Ollama) the operator can read at a
  glance.
* Measure latency end-to-end (``time.monotonic`` delta wrapping the
  network call).

The probes are deliberately tiny (one round-trip each). The
orchestrator may call them dozens of times during normal operation
(every ``infrastructure_status`` call re-probes) — anything heavier
would cost real wall-time per MCP tool invocation.

A note on the Rhea MCP probe
----------------------------
:func:`rhea_mcp_probe` reuses :class:`MCPTransport` from nanobrain
rather than reimplementing the MCP wire protocol. The protocol has
session-handshake semantics (``initialize`` + ``notifications/initialized``
+ session-id header) that we MUST get right to know the server is
healthy — and the framework already does. See
``nanobrain/library/tools/_mcp_transport.py``.

The transport caches one HTTP client per instance. Status-probe calls
construct a fresh ``MCPTransport`` per probe — that pays one MCP
handshake per call (~20-40 ms locally). That is intentional: a stale
session-id would silently mask a server restart. The slightly higher
per-probe cost is the price of correctness.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx

from apecx_integration.infrastructure.backends import ProbeResult

log = logging.getLogger(__name__)


# Default per-probe timeout. Backends override via the factory args.
_DEFAULT_TIMEOUT_S = 3.0


async def _measure(coro_factory) -> tuple[float, Any, BaseException | None]:
    """Run ``coro_factory()`` and return (latency_ms, value, exc)."""
    start = time.monotonic()
    try:
        value = await coro_factory()
        latency_ms = (time.monotonic() - start) * 1000.0
        return latency_ms, value, None
    except BaseException as exc:  # noqa: BLE001 — probe MUST capture every exception
        latency_ms = (time.monotonic() - start) * 1000.0
        return latency_ms, None, exc


# ---------------------------------------------------------------------------
# Postgres probe
# ---------------------------------------------------------------------------


async def postgres_probe(
    *,
    host: str,
    port: int,
    user: str,
    db: str,
    password: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """Probe Postgres via psycopg.

    Opens a connection, runs ``SELECT 1``, closes it. ``psycopg.connect``
    is sync; we wrap it in :func:`asyncio.to_thread` so the probe
    doesn't block the asyncio loop. A wrong-protocol hit (e.g. Redis
    on the Postgres port) is reported by psycopg as a protocol error
    string — we surface that as ``detail`` so the operator sees the
    real cause.
    """

    def _do_connect() -> None:
        # Lazy import: psycopg is in the venv but loading it costs
        # ~50ms; we don't want to pay that on import of every
        # apecx-mcp client process if probes never run.
        import psycopg

        with (
            psycopg.connect(
                host=host,
                port=port,
                user=user,
                dbname=db,
                password=password,
                connect_timeout=int(max(1, timeout_s)),
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT 1")
            cur.fetchone()

    latency_ms, _, exc = await _measure(lambda: asyncio.to_thread(_do_connect))
    if exc is None:
        return ProbeResult(
            healthy=True,
            detail=f"postgres OK on {host}:{port} (db={db}, user={user})",
            latency_ms=latency_ms,
        )
    return ProbeResult(
        healthy=False,
        detail=f"postgres probe failed on {host}:{port} (db={db})",
        latency_ms=latency_ms,
        error=f"{type(exc).__name__}: {exc}",
    )


# ---------------------------------------------------------------------------
# Redis probe
# ---------------------------------------------------------------------------


async def redis_probe(
    *,
    host: str,
    port: int,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """Probe Redis via the sync redis-py client + ``PING``.

    A wrong-protocol hit (e.g. Postgres on the Redis port) shows up
    as a ``ConnectionError`` from redis-py with the wire-bytes that
    didn't parse; we surface that.
    """

    def _do_ping() -> bool:
        import redis

        client = redis.Redis(
            host=host,
            port=port,
            socket_timeout=timeout_s,
            socket_connect_timeout=timeout_s,
        )
        try:
            return bool(client.ping())
        finally:
            with contextlib.suppress(Exception):
                client.close()

    latency_ms, value, exc = await _measure(lambda: asyncio.to_thread(_do_ping))
    if exc is None and value is True:
        return ProbeResult(
            healthy=True,
            detail=f"redis PONG on {host}:{port}",
            latency_ms=latency_ms,
        )
    if exc is None:
        return ProbeResult(
            healthy=False,
            detail=f"redis PING returned falsy value on {host}:{port}",
            latency_ms=latency_ms,
            error=f"unexpected PING response: {value!r}",
        )
    return ProbeResult(
        healthy=False,
        detail=f"redis probe failed on {host}:{port}",
        latency_ms=latency_ms,
        error=f"{type(exc).__name__}: {exc}",
    )


# ---------------------------------------------------------------------------
# MinIO probe — uses the public S3-API health endpoint, no client SDK needed
# ---------------------------------------------------------------------------


async def minio_probe(
    *,
    host: str,
    port: int,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """Probe MinIO via ``GET /minio/health/live``.

    MinIO exposes a documented health-live endpoint that returns
    HTTP 200 when the server can answer requests. Hitting it with
    plain ``httpx`` avoids pulling in the ``minio`` Python SDK
    (which the venv does not have today).
    """
    url = f"http://{host}:{port}/minio/health/live"

    async def _do_get() -> int:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
            return resp.status_code

    latency_ms, status, exc = await _measure(_do_get)
    if exc is None and status == 200:
        return ProbeResult(
            healthy=True,
            detail=f"minio OK on {host}:{port} (HTTP 200)",
            latency_ms=latency_ms,
        )
    if exc is None:
        return ProbeResult(
            healthy=False,
            detail=f"minio probe got HTTP {status} on {host}:{port}",
            latency_ms=latency_ms,
            error=f"non-200 response: {status}",
        )
    return ProbeResult(
        healthy=False,
        detail=f"minio probe failed on {host}:{port}",
        latency_ms=latency_ms,
        error=f"{type(exc).__name__}: {exc}",
    )


# ---------------------------------------------------------------------------
# Ollama probe — uses /api/tags so we can also report model count
# ---------------------------------------------------------------------------


def _model_present(names: list[str], required: str) -> bool:
    """True if ``required`` matches an installed Ollama model, tolerant of the ``:tag`` suffix
    (a configured ``mistral-nemo`` matches an installed ``mistral-nemo:latest`` and vice-versa)."""
    req_base = required.split(":", 1)[0]
    return any(n == required or n.split(":", 1)[0] == req_base for n in names)


async def ollama_probe(
    *,
    base_url: str,
    required_model: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ProbeResult:
    """Probe Ollama via ``GET /api/tags`` and report MODEL-AWARE readiness (#7).

    ``base_url`` is e.g. ``http://localhost:11434``. We tolerate a
    trailing ``/v1`` suffix (apecx's LLM env var convention) by
    stripping it — Ollama's REST API is rooted at the host, not at
    ``/v1``.

    Model-aware readiness: a *reachable* Ollama that has 0 models — or that lacks the
    ``required_model`` the synthesis runtime will ask for — is NOT usable (synthesis 404s). Such a
    server is reported ``healthy=False`` with an actionable ``ollama pull`` hint, instead of the
    old green-on-connectivity behaviour that let a model-less server look ready (the silent-failure
    trap that deferred the ollama-as-container work). ``required_model=None`` falls back to a
    "≥1 model present" floor.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}/api/tags"

    async def _do_get() -> tuple[int, Any]:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
            body = resp.json() if resp.status_code == 200 else None
            return resp.status_code, body

    latency_ms, value, exc = await _measure(_do_get)
    if exc is None and value is not None:
        status, body = value
        if status == 200:
            models = body.get("models") or [] if isinstance(body, dict) else []
            names = [m.get("name", "") for m in models if isinstance(m, dict)]
            if required_model is not None and not _model_present(names, required_model):
                return ProbeResult(
                    healthy=False,
                    detail=(
                        f"ollama up at {base} but required model {required_model!r} not present "
                        f"({len(names)} other model(s)); pull it: `ollama pull {required_model}`"
                    ),
                    latency_ms=latency_ms,
                    error=f"required model {required_model!r} missing",
                )
            if not names:
                return ProbeResult(
                    healthy=False,
                    detail=f"ollama up at {base} but 0 models loaded; pull a model to use it",
                    latency_ms=latency_ms,
                    error="no models loaded",
                )
            return ProbeResult(
                healthy=True,
                detail=f"ollama OK at {base} — {len(names)} model(s) loaded",
                latency_ms=latency_ms,
            )
        return ProbeResult(
            healthy=False,
            detail=f"ollama at {base} returned HTTP {status}",
            latency_ms=latency_ms,
            error=f"non-200 response: {status}",
        )
    return ProbeResult(
        healthy=False,
        detail=f"ollama probe failed at {base}",
        latency_ms=latency_ms,
        error=f"{type(exc).__name__}: {exc}" if exc else "unknown",
    )


# ---------------------------------------------------------------------------
# Rhea MCP probe — uses nanobrain's MCPTransport (no reimplemented wire)
# ---------------------------------------------------------------------------


async def rhea_mcp_probe(
    *,
    mcp_url: str,
    timeout_s: float = 5.0,
) -> ProbeResult:
    """Probe Rhea MCP via the canonical ``tools/list`` round-trip.

    We deliberately use ``MCPTransport`` (the same primitive Rhea's
    own dispatchers consume) rather than rolling our own JSON-RPC.
    A reachable-but-broken MCP server — e.g. a stale process that
    has lost its tool registry — will round-trip the handshake but
    return zero tools; we report that loudly via ``detail`` so the
    operator sees it.

    The probe constructs a fresh transport per call (single round-trip)
    and closes it immediately. The cost of the MCP initialize handshake
    is the price of an honest probe.
    """
    # Lazy-import so a test that doesn't touch Rhea doesn't drag the
    # nanobrain MCP transport (and its httpx setup) into memory.
    from nanobrain.library.tools._mcp_transport import MCPTransport

    transport = MCPTransport(mcp_url=mcp_url, timeout_seconds=timeout_s)

    async def _do_call() -> Any:
        try:
            return await transport.call("tools/list", {})
        finally:
            await transport.aclose()

    latency_ms, result, exc = await _measure(_do_call)
    if exc is None:
        tools = []
        if isinstance(result, dict):
            tools = result.get("tools") or []
        if isinstance(tools, list) and tools:
            return ProbeResult(
                healthy=True,
                detail=f"rhea MCP OK at {mcp_url} — {len(tools)} tool(s) listed",
                latency_ms=latency_ms,
            )
        # Reachable but reported zero tools — almost always a stale
        # process or a Rhea worker whose dependencies are down. We
        # still mark it healthy=False so the operator looks at it.
        return ProbeResult(
            healthy=False,
            detail=f"rhea MCP at {mcp_url} responded but returned 0 tools",
            latency_ms=latency_ms,
            error="empty tool catalog (server reachable but degraded)",
        )
    return ProbeResult(
        healthy=False,
        detail=f"rhea MCP probe failed at {mcp_url}",
        latency_ms=latency_ms,
        error=f"{type(exc).__name__}: {exc}",
    )


__all__ = [
    "minio_probe",
    "ollama_probe",
    "postgres_probe",
    "redis_probe",
    "rhea_mcp_probe",
]
