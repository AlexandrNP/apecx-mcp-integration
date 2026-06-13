"""On-disk JSON cache for the functional-annotation clients (E3-3, CC-4).

Mirrors the ``~/.cache/apecx_*`` discipline used by
``structural_reasoning_step._STRUCTURE_CACHE``. Immutable sources (PDB/SIFTS) are
cached indefinitely; moving sources (UniProt by accession, IEDB by accession) carry a
TTL. The cache root is overridable with ``APECX_FUNCTIONAL_CACHE`` (tests point it at a
tmp dir to stay hermetic).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CACHE_ROOT = Path(
    os.environ.get("APECX_FUNCTIONAL_CACHE", str(Path.home() / ".cache" / "apecx_functional"))
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def cache_path(subdir: str, key: str) -> Path:
    """Return the JSON cache file path for ``key`` under ``subdir`` (created on demand).

    ``APECX_FUNCTIONAL_CACHE`` is read on every call so a test fixture that sets the env
    var after import still redirects the cache.
    """
    root = Path(
        os.environ.get("APECX_FUNCTIONAL_CACHE", str(Path.home() / ".cache" / "apecx_functional"))
    )
    d = root / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_SAFE.sub('_', key)}.json"


def read_json(path: Path, *, ttl_seconds: float | None = None) -> Any | None:
    """Read a cached JSON payload, or ``None`` when absent / expired / corrupt.

    ``ttl_seconds=None`` means cache-forever (immutable sources). A corrupt file is
    treated as a miss (never raises) so a half-written cache file degrades to a refetch.
    """
    if not path.exists():
        return None
    if ttl_seconds is not None and (time.time() - path.stat().st_mtime) > ttl_seconds:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("functional cache read failed (%s): %s", path, exc)
        return None


def write_json(path: Path, data: Any) -> None:
    """Atomically write ``data`` as JSON (tmp file + replace). Never raises on I/O error."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError as exc:
        log.warning("functional cache write failed (%s): %s", path, exc)


__all__ = ["cache_path", "read_json", "write_json", "_CACHE_ROOT"]
