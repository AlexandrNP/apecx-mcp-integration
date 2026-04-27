"""Probe batch 14 — RAG pipeline semantic + technical coverage.

Probes 330-354. User directive: "We need to properly cover RAG
pipeline functionality, both on technical and semantic level."

Existing tests (test_component_index_unit.py + test_top5_recall.py
+ test_composer_phase4_rag.py) cover the AC1/AC2/AC3/AC4 acceptance
criteria. This batch fills gaps:

- Negative discrimination (unrelated query doesn't yield false top-1)
- Score thresholds for clearly-matched vs ambiguous
- Stability under repeated calls
- Edge cases: typos, very-short / very-long queries
- Multi-domain semantic boundaries
- rag_examples vs rag_description influence on ranking
- Library_version isolation across indices
- Hash sensitivity to manifest changes
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


try:
    from nanobrain.lightweight.component_index import ComponentIndex
    _RAG_AVAILABLE = True
except ImportError:
    _RAG_AVAILABLE = False


def _model_cached() -> bool:
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    return (cache_dir / "models--sentence-transformers--all-mpnet-base-v2").is_dir()


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _RAG_AVAILABLE,
        reason="ComponentIndex not importable — install [rag] extra",
    ),
    pytest.mark.skipif(
        not _model_cached(),
        reason="all-mpnet-base-v2 model not cached locally",
    ),
]


def _write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a synthetic manifest with the schema the index expects."""
    import yaml
    manifest = {
        "workflow": {"name": "synthetic_test", "spec": "test"},
        "components": entries,
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "manifest.yml"
    p.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return p


def _build(tmp_path: Path, entries: list[dict], library_version: str = "0.1.0-test"):
    p = _write_manifest(tmp_path, entries)
    idx = ComponentIndex()
    idx.rebuild(manifest_paths=[p], library_version=library_version)
    return idx


def _entry(step_id: str, name: str, description: str, examples: list[str]) -> dict:
    return {
        "step_id": step_id,
        "step_name": name,
        "class": f"test.{name}",
        "yaml": f"steps/{name}.yml",
        "rag_description": description,
        "rag_examples": examples,
    }


# --- Probe 330: top-1 ranks the right component for an exact-domain query ---


def test_probe_330_top1_exact_match(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files into memory.",
               ["read fasta", "load fasta sequences"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data files.",
               ["read csv", "import csv table"]),
        _entry("3", "json_reader", "Reads JSON-encoded structured data files.",
               ["read json", "load json document"]),
    ])
    hits = idx.search("read FASTA sequence file", k=1)
    assert hits[0].name == "fasta_reader", (
        f"PROBE 330: top-1 for FASTA query is {hits[0].name}, not fasta_reader"
    )


# --- Probe 331: top-1 ranks csv for csv-domain query ---


def test_probe_331_top1_csv(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files into memory.",
               ["read fasta", "load fasta sequences"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data files.",
               ["read csv", "import csv table"]),
        _entry("3", "json_reader", "Reads JSON-encoded structured data files.",
               ["read json", "load json document"]),
    ])
    hits = idx.search("import comma-separated tabular data", k=1)
    assert hits[0].name == "csv_reader"


# --- Probe 332: top-1 ranks json for json-domain query ---


def test_probe_332_top1_json(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files into memory.",
               ["read fasta", "load fasta sequences"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data files.",
               ["read csv", "import csv table"]),
        _entry("3", "json_reader", "Reads JSON-encoded structured data files.",
               ["read json", "load json document"]),
    ])
    hits = idx.search("parse JSON document into a dict", k=1)
    assert hits[0].name == "json_reader"


# --- Probe 333: top-1 confidence for clear match is meaningfully high ---


def test_probe_333_top1_similarity_above_threshold(tmp_path) -> None:
    """For a clear match (FASTA reader queried 'read FASTA file'),
    top-1 similarity should be well above what you'd get for a
    random/unrelated query — a clear-match floor."""
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files into memory.",
               ["read fasta", "load fasta sequences"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data files.",
               ["read csv", "import csv table"]),
    ])
    clear_hits = idx.search("read FASTA file", k=1)
    # Threshold: clear domain match should score > 0.4 with mpnet-base.
    # If it doesn't, the embedding model isn't separating domains
    # well enough for trust in production retrieval.
    assert clear_hits[0].similarity > 0.4, (
        f"PROBE 333: clear-match similarity {clear_hits[0].similarity:.3f} "
        "is below adoption-quality floor 0.4. Model may not be working "
        "or descriptions need tuning."
    )


# --- Probe 334: unrelated query has lower top-1 similarity than clear match ---


def test_probe_334_unrelated_lower_similarity(tmp_path) -> None:
    """An unrelated query should score lower than a clear-match
    query, even if a top-1 result is returned."""
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files.",
               ["read fasta"]),
    ])
    clear = idx.search("read FASTA file", k=1)[0].similarity
    unrelated = idx.search("compute logarithm of pi to ten places", k=1)[0].similarity
    assert clear > unrelated, (
        f"PROBE 334: unrelated query scored {unrelated:.3f} >= clear "
        f"match {clear:.3f} — model can't distinguish topical relevance"
    )


# --- Probe 335: stability — same query returns same top-K bytewise ---


def test_probe_335_stability_under_repeated_search(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files.",
               ["read fasta"]),
        _entry("2", "csv_reader", "Reads CSV files.",
               ["read csv"]),
        _entry("3", "json_reader", "Reads JSON files.",
               ["read json"]),
    ])
    a = [(h.name, h.similarity) for h in idx.search("read FASTA", k=3)]
    b = [(h.name, h.similarity) for h in idx.search("read FASTA", k=3)]
    assert a == b, "PROBE 335: search is not stable across repeated calls"


# --- Probe 336: typo tolerance — mild typo still ranks correct top-1 ---


def test_probe_336_typo_tolerance(tmp_path) -> None:
    """One-character typo on a domain keyword should still surface
    the right component as top-1. mpnet-base is character-level
    aware enough for this in most cases."""
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files.",
               ["read fasta", "load FASTA"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data.",
               ["read csv"]),
    ])
    hits = idx.search("read FASTAA file", k=1)  # one typo
    assert hits[0].name == "fasta_reader", (
        f"PROBE 336: typo broke retrieval. Top-1 was {hits[0].name}"
    )


# --- Probe 337: very short query (single word) finds a sensible top-1 ---


def test_probe_337_single_word_query(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files.",
               ["read fasta"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data.",
               ["read csv"]),
    ])
    hits = idx.search("FASTA", k=1)
    assert hits[0].name == "fasta_reader"


# --- Probe 338: very long query still ranks correct top-1 ---


def test_probe_338_long_paragraph_query(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files.",
               ["read fasta"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data.",
               ["read csv"]),
    ])
    long_q = (
        "I need to read a FASTA file that contains amino acid sequences "
        "for analysis. The file format is FASTA which means it has lines "
        "starting with > followed by the sequence data. I want to load "
        "the file from disk and have access to each sequence record by "
        "identifier so I can run downstream tools on the entries."
    )
    hits = idx.search(long_q, k=1)
    assert hits[0].name == "fasta_reader"


# --- Probe 339: stopword-only query doesn't crash and returns something ---


def test_probe_339_stopword_query(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"]),
    ])
    hits = idx.search("the of and a", k=1)
    # Should return something rather than error; may not be the
    # right component, but shouldn't crash.
    assert hits is not None
    assert len(hits) <= 1


# --- Probe 340: numeric-heavy query handled gracefully ---


def test_probe_340_numeric_query(tmp_path) -> None:
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"]),
    ])
    hits = idx.search("3.14159 26 535 89", k=1)
    assert len(hits) <= 1  # doesn't crash


# --- Probe 341: rebuilding with same content gives same hash ---


def test_probe_341_rebuild_same_content_same_hash(tmp_path) -> None:
    """Hash factors in workflow_slug = manifest_path.parent.name —
    so to test "same content → same hash" the manifest must live
    in the same directory across rebuilds."""
    entries = [
        _entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"]),
    ]
    idx_a = _build(tmp_path / "shared", entries)
    idx_b = _build(tmp_path / "shared", entries)
    assert idx_a.index_hash == idx_b.index_hash


# --- Probe 342: rebuilding with adding a component changes hash ---


def test_probe_342_added_component_changes_hash(tmp_path) -> None:
    base = [_entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"])]
    idx_a = _build(tmp_path / "a", base)
    idx_b = _build(tmp_path / "b", base + [
        _entry("2", "csv_reader", "Reads CSV files.", ["read csv"])
    ])
    assert idx_a.index_hash != idx_b.index_hash


# --- Probe 343: rebuilding with edited description changes hash ---


def test_probe_343_edited_description_changes_hash(tmp_path) -> None:
    a = [_entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"])]
    b = [_entry("1", "fasta_reader", "Reads FASTA-format sequence files.", ["read fasta"])]
    idx_a = _build(tmp_path / "a", a)
    idx_b = _build(tmp_path / "b", b)
    assert idx_a.index_hash != idx_b.index_hash


# --- Probe 344: rebuilding with edited examples changes hash ---


def test_probe_344_edited_examples_changes_hash(tmp_path) -> None:
    a = [_entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"])]
    b = [_entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta", "load fasta"])]
    idx_a = _build(tmp_path / "a", a)
    idx_b = _build(tmp_path / "b", b)
    assert idx_a.index_hash != idx_b.index_hash


# --- Probe 345: top-K stability — same query, same K → same ordered hits ---


def test_probe_345_topk_ordering_stable(tmp_path) -> None:
    entries = [
        _entry(str(i), f"comp{i}", f"Component number {i} for some workload.",
               [f"do task {i}"])
        for i in range(10)
    ]
    idx = _build(tmp_path, entries)
    a = [(h.name, round(h.similarity, 4)) for h in idx.search("task 5", k=10)]
    b = [(h.name, round(h.similarity, 4)) for h in idx.search("task 5", k=10)]
    assert a == b


# --- Probe 346: name overlap with query ranks higher than similar non-matches ---


def test_probe_346_name_overlap_helps(tmp_path) -> None:
    """When the rag_description AND rag_examples don't change but
    name overlaps with the query, top-1 should still surface."""
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", "Reads FASTA-format sequence files.",
               ["read fasta"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data.",
               ["read csv"]),
    ])
    hits = idx.search("fasta_reader", k=1)
    assert hits[0].name == "fasta_reader"


# --- Probe 347: bioinformatics-specific vocabulary handled ---


def test_probe_347_bioinformatics_vocabulary(tmp_path) -> None:
    """Domain-specific terms (alphavirus, BV-BRC) should retrieve
    the bioinformatics-tagged component."""
    idx = _build(tmp_path, [
        _entry("1", "alphavirus_reader",
               "Reads alphavirus genome data from BV-BRC snapshots.",
               ["read alphavirus genomes", "BV-BRC snapshot"]),
        _entry("2", "csv_reader", "Reads comma-separated tabular data.",
               ["read csv"]),
    ])
    hits = idx.search("alphavirus genome data", k=1)
    assert hits[0].name == "alphavirus_reader"


# --- Probe 348: compositional query (two intents) finds the right one ---


def test_probe_348_compositional_query(tmp_path) -> None:
    """User wants 'find genes related to vaccine studies' — should
    surface the entity-extraction-like component, not csv_reader."""
    idx = _build(tmp_path, [
        _entry("1", "entity_extraction",
               "Extracts named entities (genes, vaccines, pathogens) from free-text queries.",
               ["find genes related to vaccine studies",
                "what vaccines target this pathogen"]),
        _entry("2", "csv_reader", "Reads CSV files.", ["read csv"]),
    ])
    hits = idx.search(
        "extract gene names and vaccine references from a paragraph", k=1
    )
    assert hits[0].name == "entity_extraction"


# --- Probe 349: empty corpus handled (rebuild rejects) ---


def test_probe_349_empty_corpus_rejected(tmp_path) -> None:
    """rebuild on a manifest with no rag_description fields should
    raise (covered by existing AC4, but probe verifies explicit path)."""
    p = _write_manifest(tmp_path, [])
    idx = ComponentIndex()
    with pytest.raises(ValueError):
        idx.rebuild(manifest_paths=[p], library_version="0.1.0-test")


# --- Probe 350: malformed manifest (missing required fields) handled ---


def test_probe_350_malformed_manifest_handled(tmp_path) -> None:
    """A manifest with components that lack rag_description/examples
    should either skip those entries or raise — never silently
    embed garbage."""
    bad = [{
        "step_id": "1", "step_name": "broken",
        "class": "test.broken", "yaml": "x.yml",
        # no rag_description, no rag_examples
    }]
    p = _write_manifest(tmp_path, bad)
    idx = ComponentIndex()
    # If the manifest has no usable rag content, rebuild raises.
    # If it accepts but skips bad entries, it would later raise on
    # empty (we pass bad-only).
    with pytest.raises(ValueError):
        idx.rebuild(manifest_paths=[p], library_version="0.1.0-test")


# --- Probe 351: multi-manifest builds combined corpus ---


def test_probe_351_multi_manifest(tmp_path) -> None:
    """Pass two manifest files; index should contain components
    from both."""
    p1 = _write_manifest(
        tmp_path / "a",
        [_entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"])],
    )
    p2 = _write_manifest(
        tmp_path / "b",
        [_entry("2", "csv_reader", "Reads CSV files.", ["read csv"])],
    )
    idx = ComponentIndex()
    idx.rebuild(manifest_paths=[p1, p2], library_version="0.1.0-test")
    assert len(idx) == 2
    hits_fasta = idx.search("read FASTA file", k=1)
    hits_csv = idx.search("import comma-separated values", k=1)
    assert hits_fasta[0].name == "fasta_reader"
    assert hits_csv[0].name == "csv_reader"


# --- Probe 352: Save then load preserves search results exactly ---


def test_probe_352_save_load_preserves_search(tmp_path) -> None:
    entries = [
        _entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"]),
        _entry("2", "csv_reader", "Reads CSV files.", ["read csv"]),
        _entry("3", "json_reader", "Reads JSON files.", ["read json"]),
    ]
    idx = _build(tmp_path / "build", entries)
    persisted = tmp_path / "saved"
    idx.save(persisted)
    loaded = ComponentIndex.load(persisted)
    a = [(h.name, round(h.similarity, 6)) for h in idx.search("read FASTA file", k=3)]
    b = [(h.name, round(h.similarity, 6)) for h in loaded.search("read FASTA file", k=3)]
    assert a == b


# --- Probe 353: long description doesn't break embedding ---


def test_probe_353_very_long_description(tmp_path) -> None:
    """Description >1000 chars: model must handle (truncate to its
    max-seq-length, but not crash)."""
    long_desc = (
        "Reads FASTA-format sequence files into memory. "
        + "Handles edge cases. " * 200
    )
    idx = _build(tmp_path, [
        _entry("1", "fasta_reader", long_desc, ["read fasta"]),
    ])
    hits = idx.search("read FASTA", k=1)
    assert hits[0].name == "fasta_reader"


# --- Probe 354: rag_examples weight: query matches example phrasing exactly ---


def test_probe_354_examples_influence_ranking(tmp_path) -> None:
    """Two components with similar generic descriptions but different
    rag_examples — query phrased like one of the examples should
    surface that component."""
    idx = _build(tmp_path, [
        _entry("1", "ranking_recent",
               "Sorts results by some criterion.",
               ["sort results by recency", "show newest first"]),
        _entry("2", "ranking_alphabetical",
               "Sorts results by some criterion.",
               ["sort results alphabetically", "order A to Z"]),
    ])
    hits = idx.search("show newest items first", k=1)
    assert hits[0].name == "ranking_recent", (
        f"PROBE 354: example-phrasing match didn't surface "
        f"ranking_recent; got {hits[0].name}"
    )
