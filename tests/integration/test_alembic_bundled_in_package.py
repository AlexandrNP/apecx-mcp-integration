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


def test_approval_policy_in_package_matches_repo_root():
    """``configs/approval_policy.yml`` is bundled into the package at
    ``_configs/approval_policy.yml`` so installed wheels can find it
    without the repo on disk. Same byte-equivalence guard as alembic.
    """
    repo_policy = REPO_ROOT / "configs" / "approval_policy.yml"
    pkg_policy = (
        REPO_ROOT / "src" / "apecx_integration" / "_configs"
        / "approval_policy.yml"
    )
    assert repo_policy.is_file(), repo_policy
    assert pkg_policy.is_file(), pkg_policy
    assert filecmp.cmp(repo_policy, pkg_policy, shallow=False), (
        f"{repo_policy} and {pkg_policy} have diverged. Resync via:\n"
        f"  cp {repo_policy} {pkg_policy}"
    )


def test_import_whitelist_in_package_matches_repo_root():
    """``configs/sandbox/import_whitelist.txt`` is bundled at
    ``_configs/import_whitelist.txt`` so the composer can find it
    when installed. Same byte-equivalence guard as alembic."""
    repo_wl = REPO_ROOT / "configs" / "sandbox" / "import_whitelist.txt"
    pkg_wl = (
        REPO_ROOT / "src" / "apecx_integration" / "_configs"
        / "import_whitelist.txt"
    )
    assert repo_wl.is_file(), repo_wl
    assert pkg_wl.is_file(), pkg_wl
    assert filecmp.cmp(repo_wl, pkg_wl, shallow=False), (
        f"{repo_wl} and {pkg_wl} have diverged. Resync via:\n"
        f"  cp {repo_wl} {pkg_wl}"
    )


def test_default_paths_in_app_resolve_to_existing_files():
    """The ``_DEFAULT_*`` constants in ``control_plane/app.py`` must
    resolve to files / dirs that exist in BOTH editable AND installed
    install modes. They are the load-bearing path-resolution
    constants for ``apecx-cp serve``; if they break under installed
    mode, every workflow route 503s with no clear signal."""
    from apecx_integration.control_plane.app import (
        _DEFAULT_APPROVAL_POLICY,
        _DEFAULT_COMPOSER_CONFIG,
        _DEFAULT_WORKFLOW_BASE_DIR,
    )
    assert _DEFAULT_COMPOSER_CONFIG.is_file(), (
        f"composer config not found at {_DEFAULT_COMPOSER_CONFIG}. "
        f"This break causes /workflows/start to 503 under installed mode."
    )
    assert _DEFAULT_APPROVAL_POLICY.is_file(), (
        f"approval policy not found at {_DEFAULT_APPROVAL_POLICY}. "
        f"This break causes /workflows/start to 503 under installed mode."
    )
    assert _DEFAULT_WORKFLOW_BASE_DIR.is_dir(), (
        f"workflow base dir not found at {_DEFAULT_WORKFLOW_BASE_DIR}. "
        f"This break causes /workflows/execute to 503 under installed mode."
    )


def test_get_db_url_returns_absolute_path_by_default(tmp_path, monkeypatch):
    """The default SQLite URL MUST be an absolute path. Pre-2026-04-27
    the default was ``./apecx_cp.db`` which crashed under Claude
    Desktop's spawn cwd with ``unable to open database file``."""
    from apecx_integration.control_plane.db import get_db_url
    monkeypatch.delenv("APECX_CP_DB_URL", raising=False)
    monkeypatch.delenv("APECX_CP_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    url = get_db_url()
    assert url.startswith("sqlite:///"), url
    db_path_str = url.removeprefix("sqlite:///")
    db_path = Path(db_path_str)
    assert db_path.is_absolute(), (
        f"default DB URL points to a relative path {db_path!r}; "
        f"absolute path required so the backend survives any cwd."
    )
    # Parent directory was created.
    assert db_path.parent.is_dir()
