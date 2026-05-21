# ruff: noqa: I001, E402
# Import order is load-bearing: ``sentence_transformers`` MUST import
# before ``faiss``. Reversing the order causes a silent segfault on
# macOS ARM during ``SentenceTransformer.encode``. See
# ``nanobrain/nanobrain/lightweight/component_index.py`` lines 1-20
# and session_friction_log.md #13 for the full diagnosis. Do not let
# an import sorter "fix" this.
"""Domain RAG index — load a pre-built FAISS IndexFlatIP + metadata.

This is a plain Python class, NOT a nanobrain component. It loads a
FAISS index produced by ``scripts/build_domain_rag_index.py`` and
exposes a single ``search(query, k)`` entry point. Model + index are
lazy-loaded on first ``search`` to keep import time cheap.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # type-only — never imported at runtime on a clean (no-[rag]) install
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)


# Lazy, order-preserving import of the optional 'rag' extra
# (sentence_transformers + faiss + numpy). These are NOT base deps — RAG is
# opt-in (G81 / ``pip install -e '.[rag]'``). Importing them at MODULE scope
# made this module — and every step that references DomainRagIndex by class
# path (DomainRagSearchStep) — un-importable on a clean install, defeating the
# opt-in and turning "RAG degrades gracefully" into "RAG crashes on import".
# Deferring the import to first real use means the module imports anywhere; a
# missing 'rag' extra degrades ``search`` to ``[]`` + a loud warning, exactly
# like a missing index file.
#
# Order is load-bearing: ``sentence_transformers`` MUST import before ``faiss``
# (macOS-ARM segfault otherwise — session_friction_log #13). Routing both
# through this one helper guarantees the order regardless of caller.
_RAG_LIBS: tuple | bool | None = None  # None=unprobed, False=unavailable, tuple=loaded


def _import_rag_libs() -> tuple:
    """Return ``(SentenceTransformer, faiss, numpy)``, importing them in the
    load-bearing order on first call (cached). Raises ``ImportError`` (also
    cached) when the optional 'rag' extra is not installed."""
    global _RAG_LIBS
    if _RAG_LIBS is False:
        raise ImportError("the 'rag' extra (sentence-transformers + faiss) is not installed")
    if _RAG_LIBS is None:
        try:
            from sentence_transformers import SentenceTransformer  # noqa: I001
            import faiss
            import numpy as np
        except ImportError:
            _RAG_LIBS = False
            raise
        _RAG_LIBS = (SentenceTransformer, faiss, np)
    return _RAG_LIBS


_DEFAULT_MODEL: str = "sentence-transformers/all-mpnet-base-v2"
# CPU keeps the index portable across macOS ARM laptops where the
# "mps" backend has caused silent crashes in adjacent code (see
# ``nanobrain.lightweight.component_index._DEFAULT_DEVICE``).
_DEFAULT_DEVICE: str = "cpu"

# Workspace root via nanobrain's G40 helper. ``fallback_depth=5`` is
# the depth from this file (agents/domain_rag/X.py) to the workspace
# root in a standard checkout — kept as a last-resort when no marker
# is found and the env var is unset. The shim that used to wrap this
# was retired 2026-05-16 (commit retires apecx_integration._workspace).
from nanobrain.library.runtime.workspace_root import locate_workflow_root  # noqa: E402

_WORKSPACE_ROOT = locate_workflow_root(
    start=__file__,
    markers=["apecx-mcp-integration"],
    env_var="APECX_WORKSPACE_ROOT",
    fallback_depth=5,
)
assert _WORKSPACE_ROOT is not None, (
    "locate_workflow_root returned None despite fallback_depth=5 — "
    "this file's parents chain is shorter than 5 levels (broken install?)"
)
_DEFAULT_INDEX_DIR: Path = _WORKSPACE_ROOT / "data" / "apecx_domain_rag"


class DomainRagIndex:
    """Pre-built domain RAG index loader + searcher.

    Usage::

        idx = DomainRagIndex()             # uses default index dir
        hits = idx.search("SARS-CoV-2 vaccine", k=5)

    The model and FAISS index are lazy-loaded on the first
    ``search`` call.

    Graceful-degradation contract (G81, 2026-05-16)
    -----------------------------------------------
    If the index directory does not exist, ``search`` returns ``[]``
    and emits one WARNING log line per process (subsequent disabled
    calls go to DEBUG to avoid log flooding). This makes RAG **optional**
    at runtime: pipelines that wire RAG branches keep running and the
    operator sees a single loud "RAG disabled" notification rather than
    a crash. ``is_available`` is the cheap stat-only probe operators
    should check before deciding whether to skip RAG steps entirely.

    To diagnose missing-index issues at install time use
    ``apecx-setup rag`` (the opt-in builder) or read
    ``data/README.md``.
    """

    def __init__(
        self,
        index_dir: Path | None = None,
        *,
        model_name: str = _DEFAULT_MODEL,
        device: str = _DEFAULT_DEVICE,
    ) -> None:
        self._index_dir: Path = Path(index_dir) if index_dir is not None else _DEFAULT_INDEX_DIR
        self._model_name: str = model_name
        self._device: str = device
        self._model: SentenceTransformer | None = None
        self._faiss_index: faiss.Index | None = None
        self._metadata: list[dict[str, Any]] | None = None
        # G81: track whether we've already loud-logged the "RAG
        # disabled" notice so a workflow that calls search() in a
        # tight loop doesn't spam WARNING lines. The first miss is
        # WARNING; subsequent misses go to DEBUG.
        self._disabled_warning_logged: bool = False

    @property
    def index_dir(self) -> Path:
        return self._index_dir

    @property
    def is_available(self) -> bool:
        """Cheap stat-only probe: True iff both the FAISS binary and
        metadata file are present on disk.

        Does NOT load the index, the sentence-transformer model, or
        validate the index's internal shape. Operators can call this
        at workflow boot to decide whether to skip RAG steps wholesale.
        """
        return (self._index_dir / "faiss_index.bin").is_file() and (
            self._index_dir / "metadata.json"
        ).is_file()

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Return up to ``k`` nearest chunks for ``query``.

        Each hit dict has keys: ``id``, ``text``, ``score``,
        ``source``, ``metadata``.

        Returns ``[]`` when the index is unavailable (graceful
        degradation per the class docstring). Raises only on real
        runtime errors (corrupted FAISS file, model-load failure,
        etc.) — never on a missing index.
        """
        if not query or not query.strip():
            return []
        if not self.is_available:
            self._log_disabled_once(reason="index")
            return []
        # Index files exist — now we genuinely need the 'rag' packages. If the
        # extra isn't installed, degrade the SAME way as a missing index
        # (loud-once warning + []), so a clean install never crashes here.
        try:
            _import_rag_libs()
        except ImportError:
            self._log_disabled_once(reason="packages")
            return []
        self._ensure_loaded()
        assert self._faiss_index is not None  # for type checker
        assert self._metadata is not None

        k_effective = min(k, len(self._metadata))
        if k_effective == 0:
            return []

        q = self._encode([query])
        sims, idxs = self._faiss_index.search(q, k_effective)

        hits: list[dict[str, Any]] = []
        for sim, idx in zip(sims[0], idxs[0], strict=True):
            if idx < 0:
                continue
            row = self._metadata[idx]
            hits.append(
                {
                    "id": row["chunk_id"],
                    "text": row["text"],
                    "score": float(max(0.0, min(1.0, sim))),
                    "source": row["source"],
                    "metadata": row.get("metadata", {}),
                }
            )
        return hits

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._faiss_index is not None and self._metadata is not None:
            return

        faiss_path = self._index_dir / "faiss_index.bin"
        meta_path = self._index_dir / "metadata.json"
        if not faiss_path.is_file() or not meta_path.is_file():
            # G81: search() pre-checks is_available, so reaching here
            # means a race (index file deleted between is_available
            # and _ensure_loaded) — surface it as a hard error so the
            # operator sees the truth instead of silent empty results.
            raise FileNotFoundError(
                f"Domain RAG index not found at {self._index_dir}. "
                "Build it first with:\n"
                "  apecx-setup rag\n"
                "or directly:\n"
                "  PYTHONPATH=src .venv/bin/python "
                "scripts/build_domain_rag_index.py"
            )

        _, faiss, _ = _import_rag_libs()
        self._metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        self._faiss_index = faiss.read_index(str(faiss_path))

    def _log_disabled_once(self, reason: str = "index") -> None:
        """Emit the loud first-time "RAG disabled" notification.

        ``reason`` selects the actionable remedy:
          * ``"index"``    — the FAISS index/metadata files are missing.
          * ``"packages"`` — the optional 'rag' extra isn't installed.

        Idempotent per instance — subsequent calls go to DEBUG so a tight loop
        of search() calls doesn't spam the WARNING channel. Both messages keep
        the ``RAG DISABLED`` marker so log scrapers match either cause.
        """
        if self._disabled_warning_logged:
            log.debug(
                "DomainRagIndex.search called but RAG unavailable (%s) at %s; returning []",
                reason,
                self._index_dir,
            )
            return
        self._disabled_warning_logged = True
        if reason == "packages":
            log.warning(
                "RAG DISABLED — the optional 'rag' extra (sentence-transformers "
                "+ faiss) is not installed, so the domain RAG index at %s cannot "
                "be loaded. search() returns empty results. Enable RAG with: "
                "`pip install -e '.[rag]'` (then build the index with "
                "`apecx-setup rag`). Pipelines that wire RAG branches continue "
                "to run with empty RAG bundles.",
                self._index_dir,
            )
            return
        log.warning(
            "RAG DISABLED — domain RAG index not present at %s. "
            "search() will return empty results until the index is built. "
            "Build it with: `apecx-setup rag` (interactive) or "
            "`PYTHONPATH=src .venv/bin/python scripts/build_domain_rag_index.py`. "
            "Pipelines that wire RAG branches continue to run; downstream "
            "synthesis steps will report empty RAG bundles in their logs.",
            self._index_dir,
        )

    def _load_model(self) -> SentenceTransformer:
        if self._model is None:
            SentenceTransformer, _, _ = _import_rag_libs()
            self._model = SentenceTransformer(self._model_name, device=self._device)
        return self._model

    def _encode(self, texts: list[str]) -> np.ndarray:
        model = self._load_model()
        arr = model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return arr.astype("float32")
