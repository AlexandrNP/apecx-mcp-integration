"""Handle store — stash a structured ``DataShape``, get back an opaque handle (EO-11).

Workflows chain by passing a handle (a short opaque string) to the next workflow rather
than routing the full structured payload through the orchestrating LLM's context. The
store keeps the payload; only the handle travels.

**Default backend** is DISK-BACKED (``DiskBackedBackend``): a small in-memory hot cache
write-through to per-handle JSON files under ``$APECX_HANDLE_STORE_DIR`` (default
``~/.apecx/handles``). Durability is load-bearing for CHAINING: a downstream workflow can
run in a SEPARATE MCP process (or after a server restart, or after the in-memory cache has
FIFO-evicted the handle) and still resolve an upstream ``data_handle`` off disk. The pure
``InMemoryBackend`` remains for callers that want session-only scope. ProxyStore (installed;
nanobrain ``DataUnitProxyRef``) is the HPC-scale backend per ``CONTRACTS.md#ext-tool-dispatch``
and is a documented swap-in via the ``HandleBackend`` protocol — deferred (no HPC-scale
payloads yet).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from apecx_integration.composition.schemas.data_shapes import DataShape, parse_data_shape


class HandleNotFound(KeyError):
    """Raised by :meth:`HandleStore.get` on an unknown handle.

    Loud by design: returning a silent ``None`` would let a caller mistake a missing
    handle for an empty result — the silent-failure shape this store avoids.
    """


class HandleBackend(Protocol):
    """Storage backend contract. v1 is in-memory; ProxyStore is a future impl."""

    def put(self, payload: dict) -> str: ...
    def get(self, handle: str) -> dict: ...
    def delete(self, handle: str) -> None: ...
    def clear(self) -> None: ...


# Default cap on retained handles. The store is a process-lifetime singleton and
# ``run_workflow`` does NOT delete handles after a run (they are kept so a later workflow
# can chain off them), so without a bound a long-lived MCP server accumulates every handle
# forever. 2000 recent handles is far more than any realistic chain depth; older handles
# FIFO-evict and a subsequent ``get`` on an evicted handle raises the LOUD ``HandleNotFound``
# (never a silent empty result).
_DEFAULT_MAX_HANDLES = 2000


class InMemoryBackend:
    """Process-lifetime, thread-safe dict backend.

    Bounded (``max_handles``, FIFO): oldest handles are evicted once the cap is reached so a
    long-lived server's memory stays bounded. Eviction is LOUD — ``get`` on an evicted handle
    raises ``HandleNotFound`` (the existing unknown-handle contract).
    """

    def __init__(self, max_handles: int = _DEFAULT_MAX_HANDLES) -> None:
        if max_handles < 1:
            raise ValueError(f"InMemoryBackend max_handles must be >= 1, got {max_handles}")
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._max_handles = max_handles

    def put(self, payload: dict) -> str:
        handle = uuid4().hex
        with self._lock:
            self._data[handle] = payload
            # Bound the store (dict is insertion-ordered → first key is oldest).
            while len(self._data) > self._max_handles:
                del self._data[next(iter(self._data))]
        return handle

    def get(self, handle: str) -> dict:
        with self._lock:
            if handle not in self._data:
                raise HandleNotFound(handle)
            return self._data[handle]

    def delete(self, handle: str) -> None:
        with self._lock:
            self._data.pop(handle, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def _handle_dir() -> Path:
    """Durable handle directory (``$APECX_HANDLE_STORE_DIR`` or ``~/.apecx/handles``), created."""
    base = Path(os.environ.get("APECX_HANDLE_STORE_DIR") or (Path.home() / ".apecx" / "handles"))
    base.mkdir(parents=True, exist_ok=True)
    return base


class DiskBackedBackend:
    """Durable backend: an in-memory hot cache write-through to per-handle JSON files.

    Unlike :class:`InMemoryBackend`, a handle SURVIVES the process — a second MCP process, a
    server restart, or a cache eviction can still resolve it by reading ``<dir>/<handle>.json``.
    This is what lets a downstream workflow (possibly a different process) chain off an upstream
    ``data_handle``. Both the cache and the on-disk set are bounded FIFO (``max_handles``; disk
    by mtime) so a long-lived server does not accumulate handle files forever. ``get`` on a
    genuinely-unknown handle still raises the LOUD ``HandleNotFound``.
    """

    def __init__(self, dir_: Path | None = None, max_handles: int = _DEFAULT_MAX_HANDLES) -> None:
        if max_handles < 1:
            raise ValueError(f"DiskBackedBackend max_handles must be >= 1, got {max_handles}")
        self._dir = dir_ or _handle_dir()
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._max_handles = max_handles

    def _path(self, handle: str) -> Path:
        return self._dir / f"{handle}.json"

    def put(self, payload: dict) -> str:
        handle = uuid4().hex
        text = json.dumps(payload, default=str)
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(handle).write_text(text, encoding="utf-8")
            self._cache[handle] = payload
            while len(self._cache) > self._max_handles:
                del self._cache[next(iter(self._cache))]
            self._prune_disk()
        return handle

    def get(self, handle: str) -> dict:
        with self._lock:
            hit = self._cache.get(handle)
        if hit is not None:
            return hit
        # Disk fallback — the durability seam that resolves cross-process / post-restart /
        # post-eviction handles the in-memory backend cannot.
        path = self._path(handle)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise HandleNotFound(handle) from exc
            with self._lock:
                self._cache[handle] = payload
            return payload
        raise HandleNotFound(handle)

    def delete(self, handle: str) -> None:
        with self._lock:
            self._cache.pop(handle, None)
        self._path(handle).unlink(missing_ok=True)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
        for path in self._dir.glob("*.json"):
            path.unlink(missing_ok=True)

    def _prune_disk(self) -> None:
        """Keep only the newest ``max_handles`` handle files (FIFO by mtime). Caller holds the lock.

        Ordering is (mtime desc, name) — the name tiebreak keeps the sort deterministic on a
        coarse-mtime filesystem where boundary-adjacent puts can share an mtime; at 2000+ handles
        a same-tick tie can still evict a just-written file, but the just-written payload also
        lives in the in-memory cache for this process, so a same-process ``get`` is unaffected.
        """
        files = sorted(
            self._dir.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True
        )
        for path in files[self._max_handles :]:
            path.unlink(missing_ok=True)


class HandleStore:
    """Typed facade: put a ``DataShape`` in, get the same typed shape back.

    The shape's ``kind`` discriminator (stored in the serialized payload) lets
    :meth:`get` reconstruct the concrete type via ``parse_data_shape``.
    """

    def __init__(self, backend: HandleBackend | None = None) -> None:
        # Default to the DURABLE backend so handles resolve across processes/restarts/eviction
        # (the workflow-chaining contract). Callers can inject InMemoryBackend for session-only scope.
        self._backend: HandleBackend = backend or DiskBackedBackend()

    def put(self, shape: DataShape) -> str:
        return self._backend.put(shape.model_dump(mode="json"))

    def get(self, handle: str) -> DataShape:
        return parse_data_shape(self._backend.get(handle))

    def delete(self, handle: str) -> None:
        self._backend.delete(handle)

    def clear(self) -> None:
        self._backend.clear()


_DEFAULT_STORE: HandleStore | None = None


def default_handle_store() -> HandleStore:
    """Process-wide store shared across MCP tool calls within one server lifetime."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = HandleStore()
    return _DEFAULT_STORE
