"""Unit tests for embedding_cache_folder — the read-only-FS-tolerant model cache dir.

Regression for the `[Errno 30] Read-only file system: '.cache'` crash in
literature_qa / literature_rag / rag_e2e_synthesis (sentence-transformers model
download with no cache_folder). The real-tool counterpart is the existing
literature_rag live tests, which call the embedding path end-to-end.
"""

from __future__ import annotations

import logging
import os
import tempfile

from apecx_integration.agents._embedding_cache import embedding_cache_folder


def test_env_override_sentence_transformers_home(monkeypatch):
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", "/custom/st-home")
    assert embedding_cache_folder() == "/custom/st-home"


def test_env_override_hf_home(monkeypatch):
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    monkeypatch.setenv("HF_HOME", "/custom/hf-home")
    assert embedding_cache_folder() == "/custom/hf-home"


def test_default_writable_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(
        "apecx_integration.agents._embedding_cache.Path.home", staticmethod(lambda: tmp_path)
    )
    got = embedding_cache_folder()
    assert got == str(tmp_path / ".cache" / "apecx_embeddings")
    assert os.path.isdir(got)  # created


def test_readonly_home_falls_back_to_tempdir(monkeypatch, caplog):
    """When ~/.cache cannot be created (read-only FS / no HOME) it degrades LOUD to a
    tempdir instead of raising [Errno 30]."""
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)

    def _no_home():
        raise RuntimeError("Path.home() could not be resolved (no HOME)")

    monkeypatch.setattr(
        "apecx_integration.agents._embedding_cache.Path.home", staticmethod(_no_home)
    )
    with caplog.at_level(logging.WARNING):
        got = embedding_cache_folder()
    assert tempfile.gettempdir() in got
    assert "apecx_embeddings" in got
    assert any("not writable" in r.message for r in caplog.records)
