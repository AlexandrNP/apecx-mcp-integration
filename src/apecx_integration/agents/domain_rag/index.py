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
from pathlib import Path
from typing import Any

from sentence_transformers import SentenceTransformer  # noqa: I001

import faiss  # noqa: E402
import numpy as np  # noqa: E402


_DEFAULT_MODEL: str = "sentence-transformers/all-mpnet-base-v2"
# CPU keeps the index portable across macOS ARM laptops where the
# "mps" backend has caused silent crashes in adjacent code (see
# ``nanobrain.lightweight.component_index._DEFAULT_DEVICE``).
_DEFAULT_DEVICE: str = "cpu"

from apecx_integration._workspace import resolve_workspace_root  # noqa: E402

_DEFAULT_INDEX_DIR: Path = (
    resolve_workspace_root(__file__, fallback_depth=5) / "data" / "apecx_domain_rag"
)


class DomainRagIndex:
    """Pre-built domain RAG index loader + searcher.

    Usage::

        idx = DomainRagIndex()             # uses default index dir
        hits = idx.search("SARS-CoV-2 vaccine", k=5)

    The model and FAISS index are lazy-loaded on the first
    ``search`` call. If the index directory does not exist, a
    ``FileNotFoundError`` is raised with the build command to run.
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

    @property
    def index_dir(self) -> Path:
        return self._index_dir

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Return up to ``k`` nearest chunks for ``query``.

        Each hit dict has keys: ``id``, ``text``, ``score``,
        ``source``, ``metadata``.
        """
        if not query or not query.strip():
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
            raise FileNotFoundError(
                f"Domain RAG index not found at {self._index_dir}. "
                "Build it first with:\n"
                "  PYTHONPATH=src .venv/bin/python "
                "scripts/build_domain_rag_index.py"
            )

        self._metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        self._faiss_index = faiss.read_index(str(faiss_path))

    def _load_model(self) -> SentenceTransformer:
        if self._model is None:
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
