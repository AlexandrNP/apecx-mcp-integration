"""Unit tests for taxdump_fetcher — NCBI taxdump downloader/extractor.

No network, no real taxdump archive.  Tests build a minimal in-memory
``taxdump.tar.gz`` containing the four required dump files (plus one
extra to verify it gets discarded) and exercise the extraction +
caching logic.

Anchors SC-A2 from
``apecx-harvesters-work/design/SYNONYM_COMPLETENESS_PLAN.md``:
- ``_WANTED`` carries 4 members (nodes.dmp, merged.dmp, names.dmp, delnodes.dmp).
- A fresh extraction produces all 4.
- A partial cache (pre-SC-A2: only nodes + merged) triggers a
  re-extraction from the cached tarball — the silent-failure-mode this
  guards is "user upgrades, names.dmp never appears, dictionary build
  silently stays at pre-SC-A2 coverage."
- An archive missing one of the 4 raises ``KeyError`` (loud, not silent).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from apecx_integration.synonym_dictionary.taxdump_fetcher import (
    _WANTED,
    fetch_taxdump,
)

# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------


def _build_taxdump_archive(
    path: Path,
    *,
    include: set[str] | None = None,
    add_extra: bool = True,
) -> None:
    """Write a minimal ``taxdump.tar.gz`` to ``path``.

    Each included file gets a 1-line placeholder body — the fetcher
    treats the .dmp files as opaque bytes and never parses them, so
    content shape is irrelevant for this unit test (the parsers are
    exercised separately in ``test_hierarchy_loader.py`` etc.).

    Parameters
    ----------
    path:
        Output tarball path.
    include:
        Subset of dump-file names to include. Defaults to all of
        ``_WANTED`` (the production case).
    add_extra:
        If True, also include a ``citations.dmp`` member to verify the
        fetcher discards non-wanted members rather than failing.
    """
    if include is None:
        include = set(_WANTED)
    payload = b"placeholder body\n"

    with tarfile.open(path, "w:gz") as tf:
        for name in sorted(include):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        if add_extra:
            extra = b"unrelated member, must be discarded\n"
            info = tarfile.TarInfo(name="citations.dmp")
            info.size = len(extra)
            tf.addfile(info, io.BytesIO(extra))


# ---------------------------------------------------------------------------
# Tests on the _WANTED contract
# ---------------------------------------------------------------------------


def test_wanted_set_contains_exactly_four_members() -> None:
    """``_WANTED`` is the source of truth for what the fetcher extracts.

    SC-A2 added ``names.dmp`` + ``delnodes.dmp`` to the prior 2-member
    set. Anyone shrinking this back to 2 members silently un-ships the
    SC-A2 work — the dictionary build keeps loading but reverts to
    OLS-only synonym coverage with no loud error.
    """
    assert {"nodes.dmp", "merged.dmp", "names.dmp", "delnodes.dmp"} == _WANTED


# ---------------------------------------------------------------------------
# Tests on extraction
# ---------------------------------------------------------------------------


def _prime_dest_with_cached_archive(
    dest: Path,
    *,
    include: set[str] | None = None,
) -> Path:
    """Pre-place ``taxdump.tar.gz`` inside ``dest`` so the fetcher's
    "archive already cached" branch fires and ``httpx`` is never called.

    Tests must avoid the ``_download`` path entirely; httpx is HTTP-only
    and would fail on any ``file://`` URL we passed it.  Pre-caching
    the archive at the expected location is the supported entry point
    that side-steps the download.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "taxdump.tar.gz"
    _build_taxdump_archive(archive, include=include)
    return archive


def test_fresh_extraction_produces_all_four_files(tmp_path: Path) -> None:
    """A pristine destination + a pre-cached complete archive yields
    all 4 paths in the documented positional order."""
    dest = tmp_path / "dest"
    _prime_dest_with_cached_archive(dest)

    nodes, merged, names, delnodes = fetch_taxdump(dest, force=False)

    # Positional contract — order matters because the workflow step
    # destructures the tuple.
    assert nodes == dest / "nodes.dmp"
    assert merged == dest / "merged.dmp"
    assert names == dest / "names.dmp"
    assert delnodes == dest / "delnodes.dmp"

    for p in (nodes, merged, names, delnodes):
        assert p.exists()
        assert p.read_bytes() == b"placeholder body\n"


def test_extra_members_are_discarded(tmp_path: Path) -> None:
    """Non-wanted members in the archive (e.g. ``citations.dmp``) do
    not appear in the destination directory.  ``_build_taxdump_archive``
    adds ``citations.dmp`` by default."""
    dest = tmp_path / "dest"
    _prime_dest_with_cached_archive(dest)

    fetch_taxdump(dest, force=False)

    assert not (dest / "citations.dmp").exists()


# ---------------------------------------------------------------------------
# Cache / skip-check upgrade-path tests
# ---------------------------------------------------------------------------


def test_full_cache_is_reused_without_re_extracting(tmp_path: Path) -> None:
    """When all 4 dump files already exist, the fetcher returns them
    without touching the tarball.  Verified by deleting the tarball
    after the first run — the second run must succeed anyway."""
    dest = tmp_path / "dest"
    _prime_dest_with_cached_archive(dest)

    fetch_taxdump(dest, force=False)
    # Wipe the cached tarball; a re-extract attempt would fail because
    # nothing in the fetcher will rebuild it (httpx would be called and
    # there is no URL to honour).
    (dest / "taxdump.tar.gz").unlink(missing_ok=True)

    # The 4 dump files are still on disk — fetcher must take the
    # skip-the-download fast path.
    nodes, merged, names, delnodes = fetch_taxdump(dest, force=False)
    assert all(p.exists() for p in (nodes, merged, names, delnodes))


def test_partial_cache_triggers_reextract(tmp_path: Path) -> None:
    """**SC-A2 upgrade-path guard.**

    A pre-SC-A2 cache has only ``nodes.dmp`` + ``merged.dmp``. The
    fetcher MUST re-extract from the cached tarball to populate the
    two new files; if it short-circuits on the partial cache, users
    upgrading silently stay on the old coverage.
    """
    dest = tmp_path / "dest"
    _prime_dest_with_cached_archive(dest)

    # Simulate a pre-SC-A2 cache by writing only the two old files
    # with stale content; the new files must not yet exist.
    (dest / "nodes.dmp").write_text("stale\n")
    (dest / "merged.dmp").write_text("stale\n")
    assert not (dest / "names.dmp").exists()
    assert not (dest / "delnodes.dmp").exists()

    fetch_taxdump(dest, force=False)

    # The two new files now exist with extracted content; the old
    # stale files were overwritten by the extraction.
    assert (dest / "names.dmp").exists()
    assert (dest / "delnodes.dmp").exists()
    assert (dest / "nodes.dmp").read_bytes() == b"placeholder body\n"


def test_missing_required_member_raises_keyerror(tmp_path: Path) -> None:
    """An archive missing one of the 4 required dump files must raise
    ``KeyError`` — silent failure here is exactly what SC-A2 exists to
    prevent (loud-fail per the workspace visibility contract)."""
    dest = tmp_path / "dest"
    _prime_dest_with_cached_archive(
        dest,
        include={"nodes.dmp", "merged.dmp", "delnodes.dmp"},
    )

    with pytest.raises(KeyError, match="names.dmp"):
        fetch_taxdump(dest, force=False)


# ---------------------------------------------------------------------------
# force=True semantics
# ---------------------------------------------------------------------------


def test_force_true_redownloads_and_reextracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``force=True`` bypasses the skip-check, re-downloads, and re-extracts.

    The real network is not exercised — we monkeypatch ``_download`` to
    materialise a known-good tarball at the destination path. The real
    download/extract is covered by
    ``tests/integration/test_taxdump_real_hierarchy.py`` (mocks
    carve-out rule: any unit-mocked behaviour has a matching real
    integration test).
    """
    dest = tmp_path / "dest"
    _prime_dest_with_cached_archive(dest)

    # Run once with force=False to populate the 4 dump files.
    fetch_taxdump(dest, force=False)

    # Capture the "good" tarball bytes for the fake download to emit.
    archive_bytes = (dest / "taxdump.tar.gz").read_bytes()

    def _fake_download(url: str, dest_archive: Path, *, show_progress: bool) -> None:
        dest_archive.write_bytes(archive_bytes)

    monkeypatch.setattr(
        "apecx_integration.synonym_dictionary.taxdump_fetcher._download",
        _fake_download,
    )

    # Mutate a dump file; force=True must overwrite it via re-extract.
    (dest / "names.dmp").write_text("mutated\n")
    fetch_taxdump(dest, force=True)

    assert (dest / "names.dmp").read_bytes() == b"placeholder body\n"
