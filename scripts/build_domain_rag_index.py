# ruff: noqa: I001, E402
# Import order is load-bearing — sentence_transformers BEFORE faiss.
# See src/apecx_integration/agents/domain_rag/index.py for the full
# rationale.
"""Build the apecx domain-specific FAISS RAG index.

Reads:
  - ``data/violin/Pathogen_Information.csv`` (one row per pathogen,
    four free-text columns chunked + tagged with pathogen name +
    NCBI taxonomy id + disease)
  - ``data/documents/*.txt`` (review-style domain documents)

Chunks at 400 chars with 50-char overlap, embeds with
``sentence-transformers/all-mpnet-base-v2`` (L2-normalized), and
writes a FAISS ``IndexFlatIP`` plus ``metadata.json`` /
``config.json`` to the output directory.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/build_domain_rag_index.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from sentence_transformers import SentenceTransformer  # noqa: I001

import faiss  # noqa: E402
import numpy as np  # noqa: E402


MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"
EMBEDDING_DIM = 768
CHUNK_SIZE = 400
CHUNK_OVERLAP = 50

VIOLIN_TEXT_COLUMNS = (
    "Pathogen_Description",
    "Microbial_Pathogenesis",
    "Host_Ranges_and_Animal_Models",
    "Host_Protective_Immunity",
)

# Bump CSV field-size limit so VIOLIN's long pathogen-description
# columns don't trigger ``_csv.Error: field larger than field
# limit``. Some columns run several KB of free text.
csv.field_size_limit(sys.maxsize)


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VIOLIN = WORKSPACE_ROOT / "data" / "violin" / "Pathogen_Information.csv"
DEFAULT_DOCS = WORKSPACE_ROOT / "data" / "documents"
DEFAULT_OUT = WORKSPACE_ROOT / "data" / "apecx_domain_rag"


def chunk_text(
    text: str,
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split ``text`` into ``size``-char chunks with ``overlap`` chars
    of carryover between adjacent chunks.

    Empty / whitespace-only text yields no chunks. Texts shorter than
    ``size`` yield exactly one chunk.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = size - overlap
    if step <= 0:
        raise ValueError(f"chunk overlap ({overlap}) must be < chunk size ({size})")
    chunks: list[str] = []
    for start in range(0, len(text), step):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(piece)
        if start + size >= len(text):
            break
    return chunks


def collect_violin_chunks(csv_path: Path) -> list[dict[str, Any]]:
    """One chunk per (row, text-column, segment) triple."""
    chunks: list[dict[str, Any]] = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            pathogen = (row.get("Pathogen") or "").strip()
            tax_id = (row.get("NCBI_Taxonomy_ID") or "").strip()
            disease = (row.get("Disease") or "").strip()
            row_id = (row.get("id") or "").strip()
            for column in VIOLIN_TEXT_COLUMNS:
                segments = chunk_text(row.get(column) or "")
                for seg_idx, seg in enumerate(segments):
                    chunks.append(
                        {
                            "chunk_id": (f"violin/{row_id}/{column}/{seg_idx}"),
                            "text": seg,
                            "source": (
                                f"violin:Pathogen_Information.csv:row={row_id}:col={column}"
                            ),
                            "metadata": {
                                "pathogen": pathogen,
                                "ncbi_taxonomy_id": tax_id,
                                "disease": disease,
                                "column": column,
                            },
                        }
                    )
    return chunks


def collect_document_chunks(docs_dir: Path) -> list[dict[str, Any]]:
    """One chunk per (file, segment) pair."""
    chunks: list[dict[str, Any]] = []
    for txt_path in sorted(docs_dir.glob("*.txt")):
        text = txt_path.read_text(encoding="utf-8")
        for seg_idx, seg in enumerate(chunk_text(text)):
            chunks.append(
                {
                    "chunk_id": f"doc/{txt_path.stem}/{seg_idx}",
                    "text": seg,
                    "source": f"document:{txt_path.name}",
                    "metadata": {
                        "pathogen": "",
                        "ncbi_taxonomy_id": "",
                        "disease": "",
                        "column": txt_path.stem,
                    },
                }
            )
    return chunks


def embed(texts: list[str]) -> np.ndarray:
    model = SentenceTransformer(MODEL_NAME, device="cpu")
    arr = model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    return arr.astype("float32")


def build(violin_csv: Path, docs_dir: Path, out_dir: Path) -> tuple[Path, int]:
    if not violin_csv.is_file():
        raise FileNotFoundError(f"VIOLIN CSV not found: {violin_csv}")
    if not docs_dir.is_dir():
        raise FileNotFoundError(f"documents dir not found: {docs_dir}")

    chunks = collect_violin_chunks(violin_csv) + collect_document_chunks(docs_dir)
    if not chunks:
        raise ValueError(
            "no chunks produced — VIOLIN columns and documents were all empty / unreadable"
        )

    print(f"[build_domain_rag_index] embedding {len(chunks)} chunks ...")
    embeddings = embed([c["text"] for c in chunks])

    index = faiss.IndexFlatIP(EMBEDDING_DIM)
    index.add(embeddings)

    out_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(out_dir / "faiss_index.bin"))
    (out_dir / "metadata.json").write_text(json.dumps(chunks, indent=2), encoding="utf-8")
    config = {
        "embedding_model": MODEL_NAME,
        "embedding_dimension": EMBEDDING_DIM,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "total_chunks": len(chunks),
        "index_type": "IndexFlatIP",
    }
    (out_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"[build_domain_rag_index] wrote {len(chunks)} chunks to {out_dir}")
    return out_dir, len(chunks)


def sanity_search(out_dir: Path, query: str = "SARS-CoV-2 vaccine") -> None:
    """Re-load the freshly built index and print top-3 hits.

    Imported lazily so a build failure surfaces before we try to
    re-load. Reuses ``DomainRagIndex`` rather than re-implementing
    the load + search path.
    """
    from apecx_integration.agents.domain_rag import DomainRagIndex

    idx = DomainRagIndex(index_dir=out_dir)
    hits = idx.search(query, k=3)
    print(f"\n[sanity] top-3 results for {query!r}:")
    for rank, hit in enumerate(hits, 1):
        preview = hit["text"][:160].replace("\n", " ")
        print(f"  {rank}. score={hit['score']:.3f} source={hit['source']}\n     {preview}...")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument(
        "--violin-pathogens",
        type=Path,
        default=DEFAULT_VIOLIN,
        help=f"VIOLIN Pathogen_Information.csv (default: {DEFAULT_VIOLIN})",
    )
    p.add_argument(
        "--documents-dir",
        type=Path,
        default=DEFAULT_DOCS,
        help=f"directory of *.txt documents (default: {DEFAULT_DOCS})",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output directory (default: {DEFAULT_OUT})",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        out_dir, _ = build(
            args.violin_pathogens.resolve(),
            args.documents_dir.resolve(),
            args.out.resolve(),
        )
        sanity_search(out_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
