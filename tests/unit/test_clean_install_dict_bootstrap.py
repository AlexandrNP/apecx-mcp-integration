"""Clean-install regression: dictionary auto-downloads with zero prior state.

Pins the contract from the workspace constraint:

    "No prior Globus information from the user side should be required
     or used."

The MCP startup gate (``_ensure_synonym_dict_or_warn``) DOWNLOADS the pre-built dictionary from
the anonymous public Globus path — that is the only auto path. On a download failure it FAILS
LOUD (it does NOT silently build); a local build runs only as an explicit dev/offline opt-in
(``APECX_DICT_ALLOW_LOCAL_BUILD=1``). These tests mock the network so they run offline; a
companion live-network test below probes the real Argonne public path when reachable.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from apecx_integration.mcp_surface.server import (
    _ensure_synonym_dict_or_warn,
    _try_public_download,
)


def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_home: Path) -> None:
    """Strip every var that could leak prior Globus / dict state.

    The dict-output dir is set explicitly to ``tmp_home/.apecx/dictionary``
    rather than via HOME because the synonym-dictionary bootstrap freezes
    its default dir at import time — a late HOME change is ignored. The
    explicit ``APECX_DICT_OUTPUT_DIR`` override is the only path that
    redirects the resolved sqlite path.
    """
    leak_prefixes = ("APECX_", "GLOBUS_", "NANOBRAIN_")
    for key in list(os.environ):
        if any(key.startswith(prefix) for prefix in leak_prefixes):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_home))
    monkeypatch.setenv("APECX_DICT_OUTPUT_DIR", str(tmp_home / ".apecx" / "dictionary"))


def test_try_public_download_opted_out_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """APECX_SKIP_DICT_DOWNLOAD=1 short-circuits to None."""
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APECX_SKIP_DICT_DOWNLOAD", "1")
    sqlite = tmp_path / "dict.sqlite"
    with patch("apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary") as mock_boot:
        result = _try_public_download(sqlite)
    assert result is None
    mock_boot.assert_not_called()


def test_try_public_download_handles_network_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bootstrap raising any error returns None so the caller falls back."""
    _clean_env(monkeypatch, tmp_path)
    sqlite = tmp_path / "dict.sqlite"
    with patch(
        "apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary",
        side_effect=RuntimeError("simulated network down"),
    ):
        result = _try_public_download(sqlite)
    assert result is None


def test_try_public_download_success_returns_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful bootstrap returns the dict path the caller exports."""
    _clean_env(monkeypatch, tmp_path)
    sqlite = tmp_path / "dict.sqlite"
    sqlite.touch()
    with patch(
        "apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary",
        return_value=sqlite,
    ):
        result = _try_public_download(sqlite)
    assert result == sqlite


def _fake_bootstrap_that_creates_file(
    dest_path: Path,
) -> callable:
    """Return a side_effect for bootstrap_dictionary that also touches
    ``dest_path`` — needed because the startup gate's second
    ``is_file()`` check would otherwise refuse the patched return value.
    """

    def _side_effect(*, dest=None, **kwargs):
        path = Path(dest) if dest else dest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"SQLite format 3\x00")
        return path

    return _side_effect


def test_ensure_synonym_dict_prefers_public_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The startup gate calls the public-download path BEFORE the local build.

    Verifies the order: clean install + zero env vars + no VIOLIN data
    must produce a working dictionary via the anonymous download, never
    touching the local-build workflow.
    """
    _clean_env(monkeypatch, tmp_path)
    fake_dict = tmp_path / ".apecx" / "dictionary" / "dictionary.sqlite"
    with (
        patch(
            "apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary",
            side_effect=_fake_bootstrap_that_creates_file(fake_dict),
        ) as mock_download,
        patch(
            "apecx_integration.synonym_dictionary.workflow.bootstrap.ensure_dictionary"
        ) as mock_build,
        patch(
            "apecx_integration.synonym_dictionary.loader.get_dictionary_index",
            return_value=(object(), None),
        ),
    ):
        _ensure_synonym_dict_or_warn()
    mock_download.assert_called_once()
    mock_build.assert_not_called()
    assert os.environ.get("APECX_SYNONYM_DICT_PATH") == str(fake_dict)


def test_download_failure_builds_ONLY_with_opt_in_else_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Download-only contract (2026-06-15): a download failure does NOT auto-build (the old
    silent fallback that masked the failure). The local build runs ONLY with
    APECX_DICT_ALLOW_LOCAL_BUILD=1; without it the server fails loud and never calls the build."""
    built_path = tmp_path / ".apecx" / "dictionary" / "dictionary.sqlite"

    def _fake_build(cfg):
        built_path.parent.mkdir(parents=True, exist_ok=True)
        built_path.write_bytes(b"SQLite format 3\x00")
        return built_path

    # (a) NO opt-in → download fails → build is NOT called.
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.delenv("APECX_DICT_ALLOW_LOCAL_BUILD", raising=False)
    with (
        patch(
            "apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary",
            side_effect=RuntimeError("simulated download failure"),
        ),
        patch(
            "apecx_integration.synonym_dictionary.workflow.bootstrap.ensure_dictionary",
            side_effect=_fake_build,
        ) as mock_build_off,
    ):
        _ensure_synonym_dict_or_warn()
    mock_build_off.assert_not_called()

    # (b) opt-in → download fails → build IS called.
    if built_path.exists():
        built_path.unlink()
    _clean_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APECX_DICT_ALLOW_LOCAL_BUILD", "1")
    with (
        patch(
            "apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary",
            side_effect=RuntimeError("simulated download failure"),
        ),
        patch(
            "apecx_integration.synonym_dictionary.workflow.bootstrap.ensure_dictionary",
            side_effect=_fake_build,
        ) as mock_build_on,
        patch(
            "apecx_integration.synonym_dictionary.loader.get_dictionary_index",
            return_value=(object(), None),
        ),
    ):
        _ensure_synonym_dict_or_warn()
    mock_build_on.assert_called_once()


def test_ensure_synonym_dict_no_globus_creds_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hardest constraint: zero Globus / APECX env vars must still produce a dict.

    Pins the workspace constraint that a clean install from a location
    with no prior data does NOT require the user to set anything Globus-
    related — neither endpoints nor client credentials nor public URL.
    """
    _clean_env(monkeypatch, tmp_path)
    # APECX_DICT_OUTPUT_DIR is the intentional set in _clean_env (see its
    # docstring); the constraint is that NO Globus-creds or credential-
    # discovery env var is required. Filter that one out of the assertion.
    leaked = [
        k
        for k in os.environ
        if k.startswith(("APECX_", "GLOBUS_")) and k != "APECX_DICT_OUTPUT_DIR"
    ]
    assert not leaked, f"clean-env setup leaked Globus/creds env: {leaked}"

    fake_dict = tmp_path / ".apecx" / "dictionary" / "dictionary.sqlite"
    with (
        patch(
            "apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary",
            side_effect=_fake_bootstrap_that_creates_file(fake_dict),
        ) as mock_download,
        patch(
            "apecx_integration.synonym_dictionary.loader.get_dictionary_index",
            return_value=(object(), None),
        ),
    ):
        _ensure_synonym_dict_or_warn()
    # bootstrap_dictionary called with NO base_url override and NO env
    # var means it reads the production default — the constraint is met.
    mock_download.assert_called_once()
    call_kwargs = mock_download.call_args.kwargs
    assert "base_url" not in call_kwargs or call_kwargs["base_url"] is None


@pytest.mark.skipif(
    os.environ.get("APECX_RUN_LIVE_DOWNLOAD_TEST") != "1",
    reason="live-network test; set APECX_RUN_LIVE_DOWNLOAD_TEST=1 to run",
)
def test_live_manifest_anonymous_reachable() -> None:
    """Live probe: the production MANIFEST.json is anonymous-reachable.

    Off by default — gated on APECX_RUN_LIVE_DOWNLOAD_TEST=1 so CI
    doesn't depend on Argonne uptime. Useful for local verification
    when investigating whether the download path itself is live.
    """
    from apecx_harvesters.dict_reader.bootstrap import fetch_manifest

    manifest = fetch_manifest(timeout=30)
    assert manifest.dictionary_filename.endswith(".sqlite.gz")
    assert manifest.dictionary_size_bytes > 1_000_000
    assert len(manifest.dictionary_sha256) == 64
