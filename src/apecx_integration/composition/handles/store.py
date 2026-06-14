"""Handle store — stash a structured ``DataShape``, get back an opaque handle (EO-11).

Workflows chain by passing a handle (a short opaque string) to the next workflow rather
than routing the full structured payload through the orchestrating LLM's context. The
store keeps the payload; only the handle travels.

**v1 backend** is an in-process dict (session-scoped — handles live for the MCP server's
lifetime; design open Q3). ProxyStore (installed; nanobrain ``DataUnitProxyRef``) is the
HPC-scale backend per ``CONTRACTS.md#ext-tool-dispatch`` and is a documented swap-in via the
``HandleBackend`` protocol — deferred because the MVP has no HPC-scale payloads and
ProxyStore's Key<->string + connector config adds friction without current benefit.
"""

from __future__ import annotations

import threading
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


class HandleStore:
    """Typed facade: put a ``DataShape`` in, get the same typed shape back.

    The shape's ``kind`` discriminator (stored in the serialized payload) lets
    :meth:`get` reconstruct the concrete type via ``parse_data_shape``.
    """

    def __init__(self, backend: HandleBackend | None = None) -> None:
        self._backend: HandleBackend = backend or InMemoryBackend()

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
