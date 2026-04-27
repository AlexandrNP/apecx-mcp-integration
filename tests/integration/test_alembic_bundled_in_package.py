"""Regression: the in-package alembic.ini + migrations/ stay byte-
equivalent to the repo-root copies.

The repo carries TWO copies of the alembic config:

  1. ``./alembic.ini`` + ``./migrations/`` at the repo root
  2. ``./src/apecx_integration/_alembic/alembic.ini`` +
     ``./src/apecx_integration/_alembic/migrations/`` inside the
     package

Why two: tests use the repo-root copy via REPO_ROOT-relative paths,
the wheel ships the in-package copy so a uv-tool / pipx / pip --user
install doesn't depend on the repo root being on disk.

This test guards drift. If a contributor updates the repo-root
alembic config without re-syncing the in-package copy (or vice
versa), this test fails with a clear message naming the
out-of-sync files.

If you intentionally change one copy, update the other in the same
commit:

    cp alembic.ini src/apecx_integration/_alembic/alembic.ini
    rsync -a --delete migrations/ src/apecx_integration/_alembic/migrations/
"""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ALEMBIC = REPO_ROOT / "src" / "apecx_integration" / "_alembic"


def test_alembic_ini_in_package_matches_repo_root():
    repo_ini = REPO_ROOT / "alembic.ini"
    pkg_ini = PKG_ALEMBIC / "alembic.ini"
    assert repo_ini.is_file(), repo_ini
    assert pkg_ini.is_file(), pkg_ini
    assert filecmp.cmp(repo_ini, pkg_ini, shallow=False), (
        f"{repo_ini} and {pkg_ini} have diverged. Resync via:\n"
        f"  cp {repo_ini} {pkg_ini}"
    )


def test_migrations_versions_in_package_match_repo_root():
    """Every alembic migration script in repo-root/migrations/versions/
    has a byte-equivalent twin in the in-package versions/ dir."""
    repo_versions = REPO_ROOT / "migrations" / "versions"
    pkg_versions = PKG_ALEMBIC / "migrations" / "versions"
    assert repo_versions.is_dir()
    assert pkg_versions.is_dir()

    repo_files = sorted(p.name for p in repo_versions.glob("*.py"))
    pkg_files = sorted(p.name for p in pkg_versions.glob("*.py"))
    assert repo_files == pkg_files, (
        f"migration version sets differ.\n"
        f"  repo-root only: {set(repo_files) - set(pkg_files)}\n"
        f"  in-package only: {set(pkg_files) - set(repo_files)}\n"
        f"Resync via: rsync -a --delete {repo_versions}/ {pkg_versions}/"
    )

    diverged = []
    for name in repo_files:
        if not filecmp.cmp(
            repo_versions / name, pkg_versions / name, shallow=False,
        ):
            diverged.append(name)
    assert not diverged, (
        f"migration scripts diverged: {diverged}. Resync via:\n"
        f"  rsync -a --delete {repo_versions}/ {pkg_versions}/"
    )


def test_migrations_env_and_template_in_package_match_repo_root():
    """env.py + script.py.mako must also stay byte-equivalent."""
    for fname in ("env.py", "script.py.mako", "README"):
        repo_file = REPO_ROOT / "migrations" / fname
        pkg_file = PKG_ALEMBIC / "migrations" / fname
        if not repo_file.is_file():
            # Some files may legitimately not exist; only check
            # equivalence when both sides have the file.
            assert not pkg_file.is_file() or pkg_file.is_file(), (
                f"{pkg_file} exists but {repo_file} does not"
            )
            continue
        assert pkg_file.is_file(), (
            f"{pkg_file} missing; resync from {repo_file}"
        )
        assert filecmp.cmp(repo_file, pkg_file, shallow=False), (
            f"{repo_file} and {pkg_file} diverged."
        )


def test_find_alembic_root_resolves_to_in_package_copy():
    """The runtime lookup MUST prefer the in-package copy. This is
    the contract that lets the wheel work from any install mode —
    breaking it (e.g., re-introducing a parent-walk-first strategy)
    silently re-breaks ``apecx-cp serve`` under uv-tool install."""
    from apecx_integration.control_plane.infra.lifecycle import (
        _find_alembic_root,
    )
    root = _find_alembic_root()
    assert (root / "alembic.ini").is_file()
    # Resolved path must be inside the package (the
    # ``_alembic`` directory under ``apecx_integration``), NOT the
    # repo root.
    assert root.name == "_alembic", (
        f"_find_alembic_root resolved to {root}, expected the "
        f"in-package _alembic directory. Walk-from-here-up is the "
        f"legacy fallback; the in-package lookup must win."
    )
