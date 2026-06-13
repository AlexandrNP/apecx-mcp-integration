"""On-disk cache for the conserved-sites ALIGN step (E3-9, CC-2 / CC-4).

Option B cache seam: cache ONLY the expensive MAFFT alignment, content-addressed on a
hash of the FETCHED sequences plus the aligner identity + the alignment-shaping params.
The fetch step always runs live, so a BV-BRC corpus change yields different sequences →
a different content hash → a cache MISS → a fresh alignment. The conservation-scoring
step also always runs, so a threshold change is always reflected. Only MAFFT (the
~6-minute end-to-end bottleneck) is skipped on a hit.

CACHE KEY determinants (everything that changes the ALIGNMENT output):
  * the aligner identity (``"mafft"``),
  * the MAFFT strategy flag (``mode``, e.g. ``--auto``) and ``--amino`` toggle,
  * the configured executable name,
  * a G24 content-hash (``nanobrain ... compute_content_hash``) of the input FASTA — so the
    actual fetched sequences drive invalidation, NOT just the taxon.
The conservation THRESHOLD is deliberately NOT in the key: it does not affect the
alignment, and the conserve step re-runs every time, so a threshold change is always
honored. This makes Option B strictly safer than caching the whole subworkflow.

Cache root: ``$APECX_CONSERVED_SITES_CACHE`` (default ``~/.cache/apecx_conserved_sites``).
Escape hatch: ``$APECX_CONSERVED_SITES_NOCACHE=1`` forces a MISS (recompute + overwrite).

RELIABILITY (CC-2 / G127): a read/write failure NEVER breaks the run — it degrades to a
normal (uncached) alignment with a warning. A corrupt entry is treated as a MISS. The
cached payload is the COMPLETE align output dict, so a HIT returns a byte-identical result
(CC-4); the cheap pass-through context (taxon_id/protein) is re-applied from the live
payload by the caller so a HIT equals a FRESH run for the same input.

RESIDUAL STALENESS (cannot be closed without running the binary): the key does NOT include
the MAFFT *version* — it can only be obtained by invoking MAFFT, which defeats the point of
skipping it. A MAFFT upgrade that changes the alignment is therefore NOT auto-invalidated;
the cached (older) alignment keeps being served. An operator forces a refresh with
``APECX_CONSERVED_SITES_NOCACHE=1`` (one run repopulates every entry).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from nanobrain.library.runtime.data_source_registry import compute_content_hash

log = logging.getLogger(__name__)

_DEFAULT_ROOT = Path.home() / ".cache" / "apecx_conserved_sites"
_SUBDIR = "align"


def nocache_enabled() -> bool:
    """True when ``$APECX_CONSERVED_SITES_NOCACHE`` is set to a truthy value (read live)."""
    return os.environ.get("APECX_CONSERVED_SITES_NOCACHE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _cache_root() -> Path:
    """Cache root, read live so a test fixture that sets the env var after import is honored."""
    return Path(os.environ.get("APECX_CONSERVED_SITES_CACHE", str(_DEFAULT_ROOT)))


def compute_sequence_set_hash(fasta_text: str) -> str:
    """Content-hash the fetched FASTA via the nanobrain G24 helper (sha256 hex, no prefix).

    Writes the FASTA bytes to a temp file and delegates to ``compute_content_hash`` rather than
    hand-rolling a digest — the same primitive that content-addresses data-source manifests, so
    a BV-BRC corpus update (different bytes) yields a different hash and invalidates the cache.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".fasta", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(fasta_text)
        tmp = Path(fh.name)
    try:
        return compute_content_hash(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def align_cache_key(
    *, aligner: str, mode: str, amino: bool, executable: str, fasta_text: str
) -> str:
    """A stable sha256 key over the alignment determinants (incl. the G24 sequence-set hash)."""
    determinants = {
        "aligner": aligner,
        "mode": mode,
        "amino": bool(amino),
        "executable": executable,
        "seq_hash": compute_sequence_set_hash(fasta_text),
    }
    canonical = json.dumps(determinants, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_path(key: str) -> Path:
    d = _cache_root() / _SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def read_cached(key: str) -> dict[str, Any] | None:
    """Return the cached align-output dict, or ``None`` on miss / corrupt / I/O error.

    Never raises (CC-2): a corrupt or unreadable entry degrades to a MISS so the caller
    recomputes instead of crashing the run.
    """
    path = _entry_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("conserved-sites align cache read failed (%s) — recomputing: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("conserved-sites align cache entry is not a dict (%s) — recomputing", path)
        return None
    return data


def write_cached(key: str, payload: dict[str, Any]) -> None:
    """Atomically write ``payload`` (tmp + replace). Never raises on I/O error (CC-2)."""
    path = _entry_path(key)
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except OSError as exc:
        log.warning("conserved-sites align cache write failed (%s): %s", path, exc)


__all__ = [
    "align_cache_key",
    "compute_sequence_set_hash",
    "nocache_enabled",
    "read_cached",
    "write_cached",
]
