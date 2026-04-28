"""
Unit tests for the ``_check_data_root_or_warn`` startup gate.

Verifies that the four scenarios surface the right log behavior:
  1. No env vars set       → loud banner, mentions env var
  2. Env var → missing dir → loud banner, mentions the path
  3. Env var → empty dir   → loud banner, mentions VIOLIN/BVBRC
  4. Env var → real data   → silent (no warning logs)
"""

import logging

import pytest
from apecx_integration.mcp_surface.server import _check_data_root_or_warn


def _reset_db_singleton() -> None:
    """Drop the cached store/error so each test re-resolves env vars."""
    import apecx_integration.mcp_surface.data.database as db

    db._store_singleton = None
    db._store_load_error = None


@pytest.fixture(autouse=True)
def clear_db_state(monkeypatch):
    monkeypatch.delenv("APECX_DATA_ROOT", raising=False)
    monkeypatch.delenv("APECX_ROOT", raising=False)
    _reset_db_singleton()
    yield
    _reset_db_singleton()


def test_no_env_vars_logs_banner(caplog):
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_data_root_or_warn()
    text = caplog.text
    assert "APECx data tools DISABLED" in text
    assert "APECX_DATA_ROOT" in text
    assert "apecx-setup" in text


def test_data_root_does_not_exist_logs_banner(monkeypatch, tmp_path, caplog):
    missing = tmp_path / "does_not_exist"
    monkeypatch.setenv("APECX_DATA_ROOT", str(missing))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_data_root_or_warn()
    assert "DISABLED" in caplog.text
    assert str(missing) in caplog.text


def test_empty_data_root_logs_banner(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("APECX_DATA_ROOT", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_data_root_or_warn()
    assert "DISABLED" in caplog.text
    assert "violin" in caplog.text.lower() or "BVBRC" in caplog.text


def test_data_present_is_silent(monkeypatch, tmp_path, caplog):
    (tmp_path / "violin").mkdir()
    (tmp_path / "BVBRC_genome_alphavirus.csv").write_text("genome_id,name\n1,test\n")
    monkeypatch.setenv("APECX_DATA_ROOT", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_data_root_or_warn()
    assert "DISABLED" not in caplog.text
    assert "apecx-setup" not in caplog.text


def test_only_violin_is_enough(monkeypatch, tmp_path, caplog):
    (tmp_path / "violin").mkdir()
    monkeypatch.setenv("APECX_DATA_ROOT", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_data_root_or_warn()
    assert "DISABLED" not in caplog.text


def test_only_bvbrc_is_enough(monkeypatch, tmp_path, caplog):
    (tmp_path / "BVBRC_genome_alphavirus.csv").write_text("x\n")
    monkeypatch.setenv("APECX_DATA_ROOT", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_data_root_or_warn()
    assert "DISABLED" not in caplog.text


def test_apecx_root_fallback(monkeypatch, tmp_path, caplog):
    (tmp_path / "data" / "violin").mkdir(parents=True)
    monkeypatch.setenv("APECX_ROOT", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="apecx_integration.mcp_surface.server"):
        _check_data_root_or_warn()
    assert "DISABLED" not in caplog.text
