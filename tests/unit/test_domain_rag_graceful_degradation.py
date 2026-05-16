"""Graceful-degradation tests for the domain RAG index (G81, 2026-05-16).

The contract under test
-----------------------
Pre-G81 the leaf ``DomainRagIndex.search`` raised ``FileNotFoundError``
when the FAISS binary or metadata file was missing. The upstream
``SynthesisContextAssemblyStep`` caught that via
``gather(return_exceptions=True)``, but other callers (a future
``DomainRagSearchStep`` wired directly, a notebook user calling
``search`` without that wrapper) would see a hard crash.

Per G81 the leaf class now:

  1. Exposes ``is_available`` — a cheap stat-only probe that does
     NOT load FAISS or the sentence-transformer model.
  2. ``search`` returns ``[]`` with a once-per-process WARNING
     when the index is unavailable (subsequent misses go to DEBUG to
     avoid log flooding).
  3. ``search`` continues to raise on real runtime errors (corrupted
     FAISS file, model-load failure, etc.) — the only failure mode
     it suppresses is "index files not present."

These tests prove that contract holds for the stated branches.
The tests use only a tmp_path index dir + caplog — no FAISS, no
sentence-transformers, no real model download. Fast (<0.5 s) and
deterministic.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_index_in(dirpath: Path) -> DomainRagIndex:  # noqa: F821 - forward
    """Build a DomainRagIndex pointed at a tmp dir.

    The import is deferred to keep the test module loadable in
    environments without ``faiss`` or ``sentence_transformers`` —
    those deps would only matter if we exercised the *real* loader,
    which these tests deliberately do not.
    """
    from apecx_integration.agents.domain_rag import DomainRagIndex

    return DomainRagIndex(index_dir=dirpath)


# ---------------------------------------------------------------------------
# is_available probe
# ---------------------------------------------------------------------------


def test_is_available_returns_false_for_missing_dir(tmp_path: Path) -> None:
    """The probe must be False when the index directory itself doesn't exist."""
    idx = _make_index_in(tmp_path / "does_not_exist")
    assert idx.is_available is False


def test_is_available_returns_false_for_empty_dir(tmp_path: Path) -> None:
    """The probe must be False when the directory exists but is empty."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    idx = _make_index_in(empty_dir)
    assert idx.is_available is False


def test_is_available_returns_false_with_only_metadata(tmp_path: Path) -> None:
    """Both files must be present; metadata.json alone is insufficient."""
    half = tmp_path / "half"
    half.mkdir()
    (half / "metadata.json").write_text("[]", encoding="utf-8")
    idx = _make_index_in(half)
    assert idx.is_available is False


def test_is_available_returns_false_with_only_faiss(tmp_path: Path) -> None:
    """Both files must be present; faiss_index.bin alone is insufficient."""
    half = tmp_path / "half2"
    half.mkdir()
    (half / "faiss_index.bin").write_bytes(b"\x00" * 16)  # not a real FAISS file
    idx = _make_index_in(half)
    assert idx.is_available is False


def test_is_available_returns_true_when_both_files_present(tmp_path: Path) -> None:
    """The probe is intentionally cheap — it does NOT validate the
    binary's internal shape, only that the files exist. A corrupted
    binary is detected later, at ``_ensure_loaded`` / first search."""
    both = tmp_path / "both"
    both.mkdir()
    (both / "faiss_index.bin").write_bytes(b"\x00" * 16)
    (both / "metadata.json").write_text("[]", encoding="utf-8")
    idx = _make_index_in(both)
    assert idx.is_available is True


# ---------------------------------------------------------------------------
# search() graceful-degradation
# ---------------------------------------------------------------------------


def test_search_returns_empty_list_when_index_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """search() must return [] (not raise) when the index files are missing."""
    idx = _make_index_in(tmp_path / "missing")

    with caplog.at_level(logging.WARNING, logger="apecx_integration.agents.domain_rag.index"):
        result = idx.search("any query", k=5)

    assert result == []
    # The once-per-process WARNING was emitted.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) >= 1, (
        f"expected at least one WARNING log line; got records: {caplog.records}"
    )
    # The message must include the "RAG DISABLED" marker and the
    # build command — operators need both to take action.
    msg = warning_records[0].getMessage()
    assert "RAG DISABLED" in msg
    assert "apecx-setup rag" in msg
    assert "build_domain_rag_index.py" in msg


def test_search_warning_fires_once_per_instance(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Second + subsequent search() calls on the same instance go to
    DEBUG, not WARNING. This protects log scrapers / dashboards from
    being flooded by a workflow that calls search() in a tight loop."""
    idx = _make_index_in(tmp_path / "missing2")

    with caplog.at_level(logging.WARNING, logger="apecx_integration.agents.domain_rag.index"):
        for _ in range(5):
            idx.search("any query", k=5)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1, (
        f"expected exactly 1 WARNING across 5 search() calls; got {len(warning_records)}"
    )


def test_search_returns_empty_for_empty_query_even_when_available(
    tmp_path: Path,
) -> None:
    """Empty queries short-circuit BEFORE any index check, regardless
    of availability. The contract is the same as pre-G81 here."""
    both = tmp_path / "both"
    both.mkdir()
    (both / "faiss_index.bin").write_bytes(b"\x00" * 16)
    (both / "metadata.json").write_text("[]", encoding="utf-8")
    idx = _make_index_in(both)

    # Empty / whitespace queries return [] without touching FAISS.
    assert idx.search("", k=5) == []
    assert idx.search("   ", k=5) == []


# ---------------------------------------------------------------------------
# Boot-time signal in domain_rag_step
# ---------------------------------------------------------------------------


def test_domain_rag_step_boot_warning_when_index_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``DomainRagSearchStep`` emits a boot-time WARNING that tells
    the operator RAG is disabled BEFORE the first query arrives.
    Catches the case where a workflow runs once and the operator
    notices the empty bundles only after the fact.

    Build the step via ``from_config(<yaml path>)`` because nanobrain
    enforces file-based configuration for Step subclasses
    (``FromConfigBase`` raises on dict input).
    """
    import yaml

    from apecx_integration.composition.steps.domain_rag_step import (
        DomainRagSearchStep,
    )

    missing = tmp_path / "missing_for_step"
    cfg_path = tmp_path / "rag_test_step.yml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "name": "rag_test",
                "class": "apecx_integration.composition.steps.domain_rag_step.DomainRagSearchStep",
                "description": "test step",
                "index_path": str(missing),
                "k": 5,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        step = DomainRagSearchStep.from_config(cfg_path)

    # The boot-time warning must be present and actionable.
    boot_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "rag" in r.getMessage().lower()
    ]
    assert boot_warnings, "expected a boot-time RAG-disabled WARNING from DomainRagSearchStep"
    joined = "\n".join(boot_warnings)
    assert "rag_chunks" in joined.lower() or "DISABLED" in joined
    assert "apecx-setup rag" in joined

    # And the step is constructible despite the missing index.
    assert step is not None
