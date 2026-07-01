"""Always-on infrastructure monitor daemon (dashboard / W3).

Polls :meth:`InfraOrchestrator.status` on an interval; when a RELOADABLE backend enters a
not-running state it auto-reloads it (:meth:`InfraOrchestrator.reload_backend`, with per-component
backoff so a persistently-broken backend is not restart-stormed) and records a :class:`FailureEvent`.
Holds the latest snapshot + a BOUNDED recent-failures buffer so the CLI and web views read ONE shared
state (long-lived-server discipline: ``deque(maxlen=...)`` + the size-capped JSONL).

v1 scope: the reloadable orchestrator backends (postgres/redis/minio/ollama/rhea). The extra
status-only components (control plane, synonym dict, RAG index, docker images) are monitored by the
views' own checks — folding them into the daemon's poll is a documented follow-up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from apecx_integration.infrastructure.failure_log import FailureEvent, InfraFailureLog

log = logging.getLogger(__name__)

# Red states we RECORD (a change into any of these is a failure worth a log line).
_RECORD_STATES = {
    "down",
    "degraded",
    "error_starting",
    "missing",
    "external_missing",
    "external_unconfigured",
}
# Reload is driven by REACHABILITY, not the state name: a backend is restarted only when its last
# probe could NOT contact it (genuinely down — e.g. a stopped container, which the orchestrator's
# re-probe marks DEGRADED-but-unreachable). A reachable-but-degraded backend (e.g. Ollama serving with
# no model) is UP — a restart would not help and would thrash — so it is recorded, not reloaded.
# Only backends we actually spawn can be reloaded; an external endpoint is operator-owned.
_RELOADABLE_KINDS = {"docker_container", "host_process"}

_DEFAULT_INTERVAL_S = 20.0
_DEFAULT_BACKOFF_S = 60.0
_RECENT_MAXLEN = 200


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class InfraMonitor:
    def __init__(
        self,
        *,
        orchestrator: Any = None,
        failure_log: InfraFailureLog | None = None,
        backoff_s: float = _DEFAULT_BACKOFF_S,
        recent_maxlen: int = _RECENT_MAXLEN,
    ) -> None:
        self._orch = orchestrator
        self._log = failure_log or InfraFailureLog()
        self._backoff_s = backoff_s
        self._last_reload_at: dict[str, float] = {}
        self._last_state: dict[str, str] = {}
        self._latest: dict[str, Any] = {}
        self._recent: deque[FailureEvent] = deque(maxlen=recent_maxlen)

    def _orchestrator(self) -> Any:
        if self._orch is not None:
            return self._orch
        # Lazy import so importing the monitor doesn't pull the orchestrator singleton up.
        from apecx_integration.infrastructure.orchestrator import get_orchestrator

        return get_orchestrator()

    @property
    def latest(self) -> dict[str, Any]:
        return self._latest

    def recent_failures(self) -> list[dict[str, Any]]:
        from dataclasses import asdict

        return [asdict(e) for e in self._recent]

    async def snapshot(self) -> dict[str, Any]:
        self._latest = await self._orchestrator().status()
        return self._latest

    async def tick(
        self, *, monotonic: float | None = None, now_iso: str | None = None
    ) -> dict[str, Any]:
        now = monotonic if monotonic is not None else time.monotonic()
        snap = await self.snapshot()
        for b in snap.get("backends", []):
            name = b.get("name", "")
            state = b.get("state", "")
            prev = self._last_state.get(name)
            self._last_state[name] = state
            if state not in _RECORD_STATES:
                continue
            attempted = False
            outcome = ""
            if (
                not b.get("reachable", True)  # genuinely down (not merely up-but-degraded)
                and b.get("kind") in _RELOADABLE_KINDS
                and (now - self._last_reload_at.get(name, -1e18)) >= self._backoff_s
            ):
                self._last_reload_at[name] = now
                attempted = True
                try:
                    result = await self._orchestrator().reload_backend(name)
                    outcome = f"reload → {result.get('state')}"
                except Exception as exc:  # noqa: BLE001 — a reload failure must not kill the daemon
                    outcome = f"reload error: {type(exc).__name__}: {exc}"
                    log.warning("monitor: reload_backend(%s) failed: %s", name, exc)
            # Record on a state CHANGE into red, or whenever we just attempted a reload (so the log
            # captures the recovery action) — NOT every tick, which would flood the sink.
            if state != prev or attempted:
                event = FailureEvent(
                    timestamp_iso=now_iso or _utc_now_iso(),
                    component=name,
                    state=state,
                    detail=b.get("detail", ""),
                    reload_attempted=attempted,
                    reload_outcome=outcome,
                )
                self._log.record(event)
                self._recent.append(event)
        return snap

    async def run_forever(
        self, *, interval_s: float = _DEFAULT_INTERVAL_S, stop_event: asyncio.Event | None = None
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — never let one bad poll kill the daemon
                log.warning("monitor tick failed: %s", exc)
            await asyncio.sleep(interval_s)


_MONITOR: InfraMonitor | None = None


def get_monitor() -> InfraMonitor:
    """Process-singleton monitor (the CLI + web views + the control-plane daemon share it)."""
    global _MONITOR
    if _MONITOR is None:
        _MONITOR = InfraMonitor()
    return _MONITOR
