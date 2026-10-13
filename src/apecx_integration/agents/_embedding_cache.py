"""Writable download-cache location for the sentence-transformers embedding model.

``SentenceTransformer(name)`` with no ``cache_folder`` falls back to the
huggingface_hub default (``$HF_HOME`` / ``~/.cache/huggingface``). In a locked-down
deploy (read-only root FS, or no resolvable ``HOME``) creating that directory fails
with ``[Errno 30] Read-only file system: '.cache'`` and the embedding step crashes —
taking ``literature_qa`` / ``literature_rag`` / ``rag_e2e_synthesis`` down with it.

This helper returns a guaranteed-writable cache dir so those tools degrade gracefully
on a read-only FS instead of crashing: it honors an explicit override first, then a
writable ``~/.cache``, then a tempdir (loud warning). The production answer is still
to set ``SENTENCE_TRANSFORMERS_HOME`` / ``HF_HOME`` to a mounted writable volume — which
this honors first — so the tempdir path is only a last-resort degrade.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def embedding_cache_folder() -> str:
    """Return a writable directory for the sentence-transformers model download.

    Order: ``SENTENCE_TRANSFORMERS_HOME`` / ``HF_HOME`` override → ``~/.cache/
    apecx_embeddings`` → a tempdir (with a loud warning). Never raises.
    """
    env = os.environ.get("SENTENCE_TRANSFORMERS_HOME") or os.environ.get("HF_HOME")
    if env:
        return env
    try:
        root = Path.home() / ".cache" / "apecx_embeddings"
        # HOME="" (common in a GUI/MCP launch) makes Path.home() non-absolute, so the
        # path resolves relative to a read-only cwd — treat that as a failure, not a dir
        # to litter cwd with. Also covers Path.home() returning "" / ".".
        if not root.is_absolute():
            raise OSError("home is not absolute (HOME empty / unresolvable)")
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    except (OSError, RuntimeError):
        # Read-only FS, or Path.home() unresolvable (no HOME) — degrade to a tempdir.
        tmp = Path(tempfile.gettempdir()) / "apecx_embeddings"
        # Best-effort: if even the tempdir can't be made, ST surfaces a clear error later.
        with contextlib.suppress(OSError):
            tmp.mkdir(parents=True, exist_ok=True)
        log.warning(
            "embedding cache ~/.cache not writable (read-only FS / no HOME); "
            "using tempdir %s — set SENTENCE_TRANSFORMERS_HOME/HF_HOME to a writable "
            "volume for a persistent cache",
            tmp,
        )
        return str(tmp)


# Every env var a HF / torch / sentence-transformers import reads to locate its cache.
# transformers + huggingface_hub resolve these AT IMPORT TIME into module constants, so a
# per-call ``cache_folder=`` cannot redirect an internal hub download once the module is
# loaded — the environment must be fixed first.
_HF_CACHE_ENV_VARS = (
    "HF_HOME",
    "SENTENCE_TRANSFORMERS_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
)


def configure_hf_cache_env() -> str:
    """Point every HF / torch cache env var at a guaranteed-writable dir (idempotent).

    MUST run before ``transformers`` / ``huggingface_hub`` / ``sentence_transformers`` are
    imported (they read their cache dir from the environment at import time). Called at the
    ``apecx-mcp`` entry so a GUI/MCP launch with an empty or read-only ``HOME`` — where
    ``os.path.join(expanduser("~"), ".cache")`` collapses to a relative ``.cache`` on a
    read-only cwd — never crashes with ``[Errno 30] Read-only file system: '.cache'``. Any
    var the operator already set is honored (``setdefault``). Returns the chosen dir.
    """
    folder = embedding_cache_folder()
    for var in _HF_CACHE_ENV_VARS:
        os.environ.setdefault(var, folder)
    return folder
