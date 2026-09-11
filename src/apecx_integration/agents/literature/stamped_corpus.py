"""Per-abstract taxon stamping + an in-memory, IRI-filterable store (skeleton).

NO FAISS, NO embeddings, NO network — the real vector sub-index that would sit
on top of this is a documented ``NotImplementedError`` stub
(``build_faiss_subindex``).
"""

from __future__ import annotations

from apecx_integration.agents.literature.gazetteer import Gazetteer


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


def build_faiss_subindex(*args, **kwargs):
    """Build a FAISS sub-index over the stamped corpus (TODO).

    Deferred: requires the ``rag`` extra (faiss / sentence-transformers), which
    would be lazy-imported HERE when implemented. Not yet available.
    """
    raise NotImplementedError(
        "build_faiss_subindex: the FAISS sub-index over stamped abstracts is a "
        "later task; it will lazy-import the 'rag' extra when implemented."
    )
