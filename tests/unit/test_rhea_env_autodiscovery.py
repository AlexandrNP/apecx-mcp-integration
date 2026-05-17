"""Unit tests for ``rhea_env_autodiscovery`` (G88, 2026-05-16).

The discovery layer is a pure-function transform on (env, filesystem)
→ env-mutations. These tests verify it:

  * Never overwrites an operator-set env var.
  * Sets RHEA_REPO_PATH only when a valid checkout is found.
  * Derives RHEA_PYTHON_PATH from REPO_PATH + venv presence.
  * Applies the macOS-specific defaults (CONDA_ENVS_DIR + PARSL backend)
    ONLY on Darwin, ONLY when unset.
  * Honors APECX_RHEA_AUTODISCOVER=0 as a wholesale opt-out.

No real rhea checkout is needed — we synthesize tiny fixture
directories with the marker files (``pyproject.toml`` +
``rhea/server/mcp_server.py``) that ``_is_rhea_repo`` validates.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_fake_rhea(root: Path) -> Path:
    """Build the minimum filesystem shape ``_is_rhea_repo`` accepts."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'rhea-mcp'\n")
    (root / "rhea" / "server").mkdir(parents=True, exist_ok=True)
    (root / "rhea" / "server" / "mcp_server.py").write_text("# fake\n")
    return root


def _make_fake_venv(root: Path) -> Path:
    """Build ``<root>/.venv/bin/python`` so RHEA_PYTHON_PATH derivation
    finds it."""
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "python").write_text('#!/bin/sh\nexec /usr/bin/env python3 "$@"\n')
    (venv_bin / "python").chmod(0o755)
    return venv_bin


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no RHEA_* / PARSL_CONTAINER_BACKEND set."""
    for var in (
        "RHEA_REPO_PATH",
        "RHEA_PYTHON_PATH",
        "RHEA_CONDA_ENVS_DIR",
        "PARSL_CONTAINER_BACKEND",
        "APECX_RHEA_AUTODISCOVER",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# RHEA_REPO_PATH discovery
# ---------------------------------------------------------------------------


def test_repo_path_unchanged_when_already_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An operator-set RHEA_REPO_PATH is NEVER overwritten — even if
    autodiscovery would have found a different valid repo."""
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        autodiscover_rhea_env,
    )

    monkeypatch.setenv("RHEA_REPO_PATH", "/operator/chose/this")
    discovered = autodiscover_rhea_env(dry_run=True)

    assert "RHEA_REPO_PATH" not in discovered
    assert os.environ["RHEA_REPO_PATH"] == "/operator/chose/this"


def test_repo_path_set_when_workspace_sibling_exists(
    tmp_path: Path,
) -> None:
    """When a valid rhea repo sits next to apecx-mcp-integration in the
    workspace layout, RHEA_REPO_PATH points at it."""
    from apecx_integration.infrastructure import rhea_env_autodiscovery as mod

    fake_repo = _make_fake_rhea(tmp_path / "workspace" / "rhea")
    fake_apecx = tmp_path / "workspace" / "apecx-mcp-integration"
    fake_apecx.mkdir(parents=True)

    # Patch the module file location so _find_rhea_repo's parents[4]
    # walk lands on tmp_path/workspace.
    fake_module_path = fake_apecx / "src" / "apecx_integration" / "infrastructure" / "x.py"
    fake_module_path.parent.mkdir(parents=True, exist_ok=True)
    fake_module_path.write_text("# fake\n")

    with patch.object(mod, "__file__", str(fake_module_path)):
        discovered = mod.autodiscover_rhea_env(dry_run=True)

    assert discovered.get("RHEA_REPO_PATH") == str(fake_repo)


def test_repo_path_unset_when_no_valid_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no rhea checkout exists in any probed location, RHEA_REPO_PATH
    is NOT set — autodiscovery silently leaves the operator's env alone
    so the orchestrator's actionable error message can fire."""
    from apecx_integration.infrastructure import rhea_env_autodiscovery as mod

    # Patch the module file so the workspace probe lands in an empty
    # tmp dir; patch HOME so the developer-location probes also miss.
    fake_module_path = (
        tmp_path
        / "empty_workspace"
        / "apecx-mcp-integration"
        / "src"
        / "apecx_integration"
        / "infrastructure"
        / "x.py"
    )
    fake_module_path.parent.mkdir(parents=True, exist_ok=True)
    fake_module_path.write_text("# fake\n")

    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))

    with patch.object(mod, "__file__", str(fake_module_path)):
        discovered = mod.autodiscover_rhea_env(dry_run=True)

    assert "RHEA_REPO_PATH" not in discovered


# ---------------------------------------------------------------------------
# RHEA_PYTHON_PATH derivation
# ---------------------------------------------------------------------------


def test_python_path_derived_when_venv_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With REPO_PATH set + a real .venv/bin/python in place, PYTHON_PATH
    derives correctly."""
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        autodiscover_rhea_env,
    )

    repo = _make_fake_rhea(tmp_path / "rhea")
    venv_bin = _make_fake_venv(repo)
    monkeypatch.setenv("RHEA_REPO_PATH", str(repo))

    discovered = autodiscover_rhea_env(dry_run=True)
    assert discovered.get("RHEA_PYTHON_PATH") == str(venv_bin)


def test_python_path_not_derived_when_venv_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPO_PATH set but no .venv yet (`uv sync` not run) → PYTHON_PATH
    stays unset. The orchestrator's actionable message tells the operator
    to run `apecx-setup rhea`."""
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        autodiscover_rhea_env,
    )

    repo = _make_fake_rhea(tmp_path / "rhea")
    # No .venv/bin/python created.
    monkeypatch.setenv("RHEA_REPO_PATH", str(repo))

    discovered = autodiscover_rhea_env(dry_run=True)
    assert "RHEA_PYTHON_PATH" not in discovered


# ---------------------------------------------------------------------------
# macOS defaults
# ---------------------------------------------------------------------------


def test_macos_defaults_applied_only_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Darwin: CONDA_ENVS_DIR + PARSL_CONTAINER_BACKEND get set.
    On Linux: neither gets set (rhea's in-tree defaults work there)."""
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        autodiscover_rhea_env,
    )

    # Force Darwin path.
    with patch("platform.system", return_value="Darwin"):
        discovered = autodiscover_rhea_env(dry_run=True)
    assert "RHEA_CONDA_ENVS_DIR" in discovered
    assert "apecx-rhea" in discovered["RHEA_CONDA_ENVS_DIR"]
    assert discovered.get("PARSL_CONTAINER_BACKEND") == "local"

    # Force Linux path.
    with patch("platform.system", return_value="Linux"):
        discovered = autodiscover_rhea_env(dry_run=True)
    assert "RHEA_CONDA_ENVS_DIR" not in discovered
    assert "PARSL_CONTAINER_BACKEND" not in discovered


def test_macos_defaults_honor_operator_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-set CONDA_ENVS_DIR / PARSL_CONTAINER_BACKEND is never
    overwritten by the macOS defaults."""
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        autodiscover_rhea_env,
    )

    monkeypatch.setenv("RHEA_CONDA_ENVS_DIR", "/opt/operator/conda")
    monkeypatch.setenv("PARSL_CONTAINER_BACKEND", "docker")

    with patch("platform.system", return_value="Darwin"):
        discovered = autodiscover_rhea_env(dry_run=True)

    assert "RHEA_CONDA_ENVS_DIR" not in discovered
    assert "PARSL_CONTAINER_BACKEND" not in discovered
    assert os.environ["RHEA_CONDA_ENVS_DIR"] == "/opt/operator/conda"
    assert os.environ["PARSL_CONTAINER_BACKEND"] == "docker"


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------


def test_autodiscovery_opt_out_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """APECX_RHEA_AUTODISCOVER=0 makes autodiscovery_enabled return False."""
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        autodiscovery_enabled,
    )

    monkeypatch.setenv("APECX_RHEA_AUTODISCOVER", "0")
    assert autodiscovery_enabled() is False

    monkeypatch.setenv("APECX_RHEA_AUTODISCOVER", "1")
    assert autodiscovery_enabled() is True

    monkeypatch.delenv("APECX_RHEA_AUTODISCOVER", raising=False)
    assert autodiscovery_enabled() is True  # default = enabled
