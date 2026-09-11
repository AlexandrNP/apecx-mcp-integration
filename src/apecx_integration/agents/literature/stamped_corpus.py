"""Per-abstract taxon stamping + an in-memory, IRI-filterable store, plus a
lazy FAISS vector sub-index (``build_faiss_subindex``) over stamped records.

The FAISS path lazy-imports the optional ``rag`` extra INSIDE the function so
this module imports fine on a clean install (see repo CLAUDE.md → "Clean-install:
never module-scope-import an optional extra"). Import order is load-bearing:
``sentence_transformers`` before ``faiss`` (macOS-ARM segfault otherwise).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apecx_integration.agents.literature.gazetteer import Gazetteer

if TYPE_CHECKING:  # type-only — never imported at runtime on a clean (no-[rag]) install
    import faiss
    from sentence_transformers import SentenceTransformer


def stamp_abstract(record: dict, gazetteer: Gazetteer) -> dict:
    """Return ``record`` plus an ``iris`` list: deduped IRIs (first-seen order)
    from tagging title + abstract."""
    text = f"{record.get('title', '')} {record.get('abstract', '')}"
    iris: list[str] = []
    for tag in gazetteer.tag(text):
        if tag.iri not in iris:
            iris.append(tag.iri)
    return {**record, "iris": iris}


class StampedCorpus:
    """In-memory store of stamped records, filterable by taxon IRI."""

    def __init__(self) -> None:
        self._records: list[dict] = []

    def add(self, record: dict) -> None:
        self._records.append(record)

    def filter_by_iri(self, iri: str) -> list[dict]:
        return [r for r in self._records if iri in r.get("iris", [])]


_DEFAULT_MODEL = "sentence-transformers/all-mpnet-base-v2"


def _record_text(record: dict, text_key: str) -> str:
    """``"{title}. {abstract}"`` when both exist; else whichever one does."""
    title = (record.get("title") or "").strip()
    abstract = (record.get(text_key) or "").strip()
    if title and abstract:
        return f"{title}. {abstract}"
    return title or abstract


class SubIndex:
    """A tiny cosine-similarity FAISS sub-index over a list of records.

    Built by :func:`build_faiss_subindex`; not constructed directly by callers.
    ``search`` returns the top-k records (copies) annotated with a ``score``.
    An empty corpus yields an index whose ``search`` returns ``[]``.
    """

    def __init__(
        self,
        records: list[dict],
        index: faiss.Index | None,
        model: SentenceTransformer | None,
    ) -> None:
        self._records = records
        self._index = index
        self._model = model

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        if self._index is None or not query or not query.strip():
            return []
        k_effective = min(k, len(self._records))
        if k_effective == 0:
            return []
        q = self._model.encode(
            [query],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype("float32")
        sims, idxs = self._index.search(q, k_effective)
        hits: list[dict[str, Any]] = []
        for sim, idx in zip(sims[0], idxs[0], strict=True):
            if idx < 0:
                continue
            hits.append({**self._records[idx], "score": float(sim)})
        return hits


def build_faiss_subindex(
    records: list[dict],
    *,
    text_key: str = "abstract",
    model_name: str = _DEFAULT_MODEL,
    model: SentenceTransformer | None = None,
) -> SubIndex:
    """Build a cosine-similarity FAISS sub-index over ``records``.

    Each record's text is ``"{title}. {abstract}"`` (falling back to whichever
    field exists; ``text_key`` names the abstract field). Texts are embedded
    with all-mpnet-base-v2 (normalized, cpu) and stored in a ``IndexFlatIP`` —
    the same embed pattern as ``DomainRagIndex``. Pass a preloaded ``model`` to
    avoid re-loading it (e.g. in tests).

    Empty ``records`` returns a ``SubIndex`` whose ``search`` returns ``[]``.

    Raises ``ImportError`` (with an install hint) when the optional ``rag``
    extra is not installed. Import order is load-bearing — see module docstring.
    """
    if not records:
        return SubIndex([], None, None)

    try:
        from sentence_transformers import SentenceTransformer  # noqa: I001 — order load-bearing
        import faiss
    except ImportError as e:
        raise ImportError(
            "build_faiss_subindex requires the optional 'rag' extra "
            "(sentence-transformers + faiss). Install it with: pip install '.[rag]'"
        ) from e

    if model is None:
        model = SentenceTransformer(model_name, device="cpu")

    texts = [_record_text(r, text_key) for r in records]
    embeddings = model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    ).astype("float32")

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    return SubIndex(list(records), index, model)
