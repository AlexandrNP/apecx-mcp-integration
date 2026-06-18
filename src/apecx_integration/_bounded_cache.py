"""A tiny FIFO-bounded mapping for process-lifetime memo caches.

A plain ``dict`` memoizer keyed by unbounded inputs (per-query LLM verdicts, per-term OLS
lookups) grows without limit over a long-lived MCP server's lifetime — a slow leak toward OOM.
``BoundedDict`` caps the entry count, evicting the oldest-inserted key on overflow. It is a
drop-in for ``dict`` for the memoize pattern (``key in cache`` / ``cache[key]`` get+set /
``cache.clear()``).
"""

from __future__ import annotations

from collections import OrderedDict


class BoundedDict(OrderedDict):
    """``dict`` that keeps at most ``maxsize`` entries, evicting the oldest-inserted on overflow."""

    def __init__(self, maxsize: int = 512) -> None:
        if maxsize < 1:
            raise ValueError(f"BoundedDict maxsize must be >= 1, got {maxsize}")
        super().__init__()
        self._maxsize = int(maxsize)

    def __setitem__(self, key, value) -> None:
        # Re-insert an existing key at the end so the eviction window tracks recency of write.
        if key in self:
            super().__delitem__(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            super().__delitem__(next(iter(self)))


__all__ = ["BoundedDict"]
