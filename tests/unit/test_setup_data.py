"""
Unit tests for apecx_integration.cli.setup_data.

Verifies: gh availability checks, download dispatch, extraction, and
the missing-file warning path.  Does NOT shell out to GitHub or gh.
"""

import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from apecx_integration.cli.setup_data import (
    _EXPECTED_FILES,
    _gh_authenticated,
    _gh_available,
    main,
)


# ---------------------------------------------------------------------------
# _gh_available
# ---------------------------------------------------------------------------
def test_gh_available_when_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    assert _gh_available() is True


def test_gh_available_when_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert _gh_available() is False


# ---------------------------------------------------------------------------
# _gh_authenticated
# ---------------------------------------------------------------------------
def test_gh_authenticated_success(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0),
    )
    assert _gh_authenticated() is True


def test_gh_authenticated_failure(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: MagicMock(returncode=1),
    )
    assert _gh_authenticated() is False


# ---------------------------------------------------------------------------
# main() — error paths
# ---------------------------------------------------------------------------
def test_main_exits_when_gh_missing(monkeypatch, capsys):
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "gh" in capsys.readouterr().out.lower()


def test_main_exits_when_not_authenticated(monkeypatch, capsys):
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "auth" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# main() — happy path: downloads, extracts, prints APECX_DATA_ROOT hint
# ---------------------------------------------------------------------------
def test_main_happy_path(monkeypatch, tmp_path, capsys):
    # Build a real tarball with the expected file layout in a temp dir.
    archive_dir = tmp_path / "archive_src"
    archive_dir.mkdir()
    (archive_dir / "violin").mkdir()
    for f in _EXPECTED_FILES:
        (archive_dir / f).write_text("fake,csv,data\n")
    archive_path = tmp_path / "apecx-data.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:  # noqa: S202
        for f in _EXPECTED_FILES:
            tf.add(archive_dir / f, arcname=f)

    dest_dir = tmp_path / "data"

    # Patch: gh checks pass, input returns dest_dir, download copies archive.
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: str(dest_dir))

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)

    main()

    out = capsys.readouterr().out
    assert "All 6 data files extracted successfully" in out
    assert "APECX_DATA_ROOT" in out
    # All files exist on disk.
    for f in _EXPECTED_FILES:
        assert (dest_dir / f).exists(), f"Expected {f} in {dest_dir}"


# ---------------------------------------------------------------------------
# main() — CalledProcessError from gh triggers sys.exit(1)
# ---------------------------------------------------------------------------
def test_main_download_failure_exits(monkeypatch, tmp_path, capsys):
    dest_dir = tmp_path / "data"
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: str(dest_dir))

    def failing_download(_dest: str) -> None:
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", failing_download)

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "download failed" in capsys.readouterr().out
