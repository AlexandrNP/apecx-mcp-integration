"""#1 (2026-07-01) — PyMOL adapter fails LOUD with a reinstall hint when its container artifacts
are missing.

A stale / incomplete install (an MCP server process still running a pre-fix build — the reported
``docker/pymol/_pymol_job.py`` FileNotFoundError — or a wheel that didn't ship the
``_pymol_container/**`` package-data) used to surface as a BARE FileNotFoundError from
``shutil.copy2`` deep inside the run. ``_verify_artifacts_present`` now raises early with an
actionable "reinstall + restart" message; the startup build-stamp (server.py) makes the staleness
visible in the first place.
"""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.composition.steps.pymol_sasa_tool import PyMOLToolBackendAdapter


def test_verify_artifacts_present_raises_reinstall_hint_when_missing(tmp_path):
    adapter = PyMOLToolBackendAdapter(
        dockerfile_path=str(tmp_path / "nope" / "Dockerfile"),
        job_script=tmp_path / "nope" / "_pymol_job.py",
        sasa_helper=tmp_path / "nope" / "_pymol_sasa.py",
    )
    with pytest.raises(RuntimeError) as exc:
        adapter._verify_artifacts_present()
    msg = str(exc.value)
    assert "PyMOL container artifacts not found" in msg
    assert "uv tool install --reinstall" in msg
    assert "_pymol_job.py" in msg  # names the missing file so the operator can see what's gone


def test_verify_artifacts_present_passes_for_the_real_packaged_artifacts():
    # The default adapter points at the shipped _pymol_container/ artifacts — must NOT raise.
    PyMOLToolBackendAdapter()._verify_artifacts_present()


def test_ensure_image_verifies_artifacts_before_building(tmp_path):
    # ensure_image must fail-loud on missing artifacts BEFORE it attempts a docker build (so the
    # failure is the actionable reinstall hint, not a downstream docker/copy error).
    adapter = PyMOLToolBackendAdapter(
        dockerfile_path=str(tmp_path / "Dockerfile"),  # missing
        job_script=tmp_path / "_pymol_job.py",
        sasa_helper=tmp_path / "_pymol_sasa.py",
    )
    with pytest.raises(RuntimeError, match="artifacts not found"):
        asyncio.run(adapter.ensure_image())


def test_package_version_resolver_returns_a_string():
    # The build-stamp startup log (server.py main) depends on this resolver returning a str.
    from apecx_integration.mcp_surface.server import _resolve_package_version

    v = _resolve_package_version()
    assert isinstance(v, str) and v  # a version string or the honest "unknown"
