"""Regression: the PubMed harvester must not crash on a read-only filesystem.

``apecx_harvesters`` defaults its ``cache_root`` to a CWD-relative ``.cache``; on a
read-only mount its ``_cache_save`` mkdir raised ``OSError [Errno 30] Read-only file
system: '.cache'`` and crashed the whole literature path (literature_qa / literature_rag
/ the synthesis PubMed branch). ``_harvester_cache_kwargs`` resolves a writable ABSOLUTE
root, or disables caching if nowhere is writable.

Mocks-parity: the real read-only path is verified manually by running ``literature_qa``
from a read-only cwd (``cd / && … literature_qa(...)`` → no OSError); this test pins the
resolver logic deterministically.
"""

from __future__ import annotations

import logging
import pathlib
from pathlib import Path

from apecx_integration.composition.steps._pubmed_helpers import _harvester_cache_kwargs


def test_picks_writable_absolute_cache_root_by_default():
    kw = _harvester_cache_kwargs()
    assert set(kw) == {"cache_root"}
    root = Path(kw["cache_root"])
    assert root.is_absolute() and root.is_dir()
    probe = root / ".pytest_write_probe"
    probe.touch()
    probe.unlink()


def test_env_override_is_honored(monkeypatch, tmp_path):
    target = tmp_path / "harvester_cache"
    monkeypatch.setenv("APECX_HARVESTER_CACHE", str(target))
    kw = _harvester_cache_kwargs()
    assert kw == {"cache_root": str(target)}
    assert target.is_dir()


def test_degrades_to_use_cache_false_when_all_readonly(monkeypatch, caplog):
    """Every candidate root unwritable (read-only FS) ⇒ disable caching, don't crash."""

    def _boom(self, *args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(pathlib.Path, "mkdir", _boom)
    with caplog.at_level(logging.WARNING):
        kw = _harvester_cache_kwargs()
    assert kw == {"use_cache": False}
    assert any(
        "read-only" in r.message.lower() or "disabling" in r.message.lower() for r in caplog.records
    )
