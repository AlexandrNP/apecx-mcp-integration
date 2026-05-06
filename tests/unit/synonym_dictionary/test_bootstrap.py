"""Unit tests for the dictionary-build bootstrap entry point.

These tests pin the FAST PATH (idempotency, opt-out, missing-data) so
the costly live-OLS build path can be exercised separately by the
integration test suite. No nanobrain workflow is actually driven here —
the slow path is mocked out at the module boundary.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

from apecx_integration.synonym_dictionary.workflow.bootstrap import (
    EnsureDictionaryConfig,
    ensure_dictionary,
    ensure_dictionary_async,
)


@pytest.fixture()
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reset every env var the bootstrap reads."""
    for key in (
        "APECX_SYNONYM_DICT_PATH",
        "APECX_DATA_ROOT",
        "APECX_TAXDUMP_DIR",
        "APECX_DICT_OUTPUT_DIR",
        "APECX_SKIP_DICT_BUILD",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_resolve_uses_env_var_for_sqlite_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APECX_SYNONYM_DICT_PATH", "/explicit/path/dict.sqlite")
    cfg = EnsureDictionaryConfig().resolve()
    assert cfg.sqlite_path == Path("/explicit/path/dict.sqlite")


def test_resolve_derives_sqlite_from_output_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APECX_SYNONYM_DICT_PATH", raising=False)
    monkeypatch.setenv("APECX_DICT_OUTPUT_DIR", "/some/output")
    cfg = EnsureDictionaryConfig().resolve()
    assert cfg.sqlite_path == Path("/some/output/dictionary.sqlite")


def test_idempotent_when_sqlite_exists(isolated_paths: Path) -> None:
    """Already-built dictionary returns its path without driving a workflow."""
    sqlite = isolated_paths / "dictionary.sqlite"
    sqlite.touch()

    with mock.patch(
        "apecx_integration.synonym_dictionary.workflow.bootstrap._drive_workflow"
    ) as drive:
        result = ensure_dictionary(EnsureDictionaryConfig(sqlite_path=sqlite))

    assert result == sqlite
    drive.assert_not_called()


def test_skip_via_env_var(isolated_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """APECX_SKIP_DICT_BUILD=1 → return None, no build attempted."""
    sqlite = isolated_paths / "missing.sqlite"  # doesn't exist
    monkeypatch.setenv("APECX_SKIP_DICT_BUILD", "1")

    with mock.patch(
        "apecx_integration.synonym_dictionary.workflow.bootstrap._drive_workflow"
    ) as drive:
        result = ensure_dictionary(EnsureDictionaryConfig(sqlite_path=sqlite))

    assert result is None
    drive.assert_not_called()


def test_skip_when_data_missing(isolated_paths: Path) -> None:
    """No VIOLIN files under data_root → skip with warning, return None."""
    sqlite = isolated_paths / "out" / "dictionary.sqlite"  # doesn't exist
    data_root = isolated_paths / "empty_data"
    data_root.mkdir()  # exists but no violin/ subtree

    cfg = EnsureDictionaryConfig(
        sqlite_path=sqlite,
        data_root=data_root,
        skip_if_data_missing=True,
    )

    with mock.patch(
        "apecx_integration.synonym_dictionary.workflow.bootstrap._drive_workflow"
    ) as drive:
        result = ensure_dictionary(cfg)

    assert result is None
    drive.assert_not_called()


def test_skip_can_be_disabled(isolated_paths: Path) -> None:
    """skip_if_data_missing=False forces the workflow path even with no data;
    we mock _drive_workflow so the test doesn't actually try to build."""
    sqlite = isolated_paths / "out" / "dictionary.sqlite"  # doesn't exist
    data_root = isolated_paths / "empty_data"
    data_root.mkdir()

    cfg = EnsureDictionaryConfig(
        sqlite_path=sqlite,
        data_root=data_root,
        skip_if_data_missing=False,
    )

    async def fake_drive(_cfg):
        sqlite.parent.mkdir(parents=True, exist_ok=True)
        sqlite.touch()
        return sqlite

    with mock.patch(
        "apecx_integration.synonym_dictionary.workflow.bootstrap._drive_workflow",
        side_effect=fake_drive,
    ) as drive:
        result = ensure_dictionary(cfg)

    assert result == sqlite
    drive.assert_called_once()


def test_drive_path_runs_workflow_when_inputs_present(isolated_paths: Path) -> None:
    """At least one VIOLIN file present + sqlite missing → workflow driven."""
    sqlite = isolated_paths / "out" / "dictionary.sqlite"
    data_root = isolated_paths / "data"
    (data_root / "violin").mkdir(parents=True)
    (data_root / "violin" / "Pathogen_Information.csv").write_text("name\nfoo\n")

    cfg = EnsureDictionaryConfig(sqlite_path=sqlite, data_root=data_root)

    async def fake_drive(_cfg):
        sqlite.parent.mkdir(parents=True, exist_ok=True)
        sqlite.touch()
        return sqlite

    with mock.patch(
        "apecx_integration.synonym_dictionary.workflow.bootstrap._drive_workflow",
        side_effect=fake_drive,
    ) as drive:
        result = ensure_dictionary(cfg)

    assert result == sqlite
    drive.assert_called_once()


@pytest.mark.asyncio
async def test_async_entrypoint_idempotent(isolated_paths: Path) -> None:
    """The async variant honors the same idempotency contract."""
    sqlite = isolated_paths / "dictionary.sqlite"
    sqlite.touch()

    result = await ensure_dictionary_async(EnsureDictionaryConfig(sqlite_path=sqlite))
    assert result == sqlite


def test_drive_workflow_chdirs_to_writable_dir_and_restores(
    isolated_paths: Path,
) -> None:
    """Regression test for the nanobrain logs/ cwd bug (2026-05-06).

    nanobrain's async_logging falls back to ``Path("logs")`` (cwd-relative).
    If apecx-mcp is launched with cwd=`/` (read-only on macOS), the workflow
    instantiation crashes with ``[Errno 30] Read-only file system: 'logs'``.
    ``_drive_workflow`` must chdir to a writable dir BEFORE
    ``Workflow.from_config`` is called, and restore the original cwd in
    ``finally`` — even if the workflow raises.

    Strategy: stub ``Workflow.from_config`` itself to capture cwd at call
    time, then raise. The real chdir/restore code in bootstrap is what's
    being verified.
    """
    sqlite = isolated_paths / "out" / "dictionary.sqlite"
    data_root = isolated_paths / "data"
    (data_root / "violin").mkdir(parents=True)
    (data_root / "violin" / "Pathogen_Information.csv").write_text("name\nfoo\n")

    cwd_at_workflow_call: list[str] = []
    prev_cwd = os.getcwd()

    class _StubFromConfig(Exception):
        pass

    def _capture_cwd_then_raise(*args, **kwargs):
        cwd_at_workflow_call.append(os.getcwd())
        raise _StubFromConfig("captured cwd; abort the workflow path")

    fake_workflow_cls = mock.MagicMock()
    fake_workflow_cls.from_config = mock.MagicMock(side_effect=_capture_cwd_then_raise)

    cfg = EnsureDictionaryConfig(sqlite_path=sqlite, data_root=data_root)

    # ensure_dictionary lets the underlying exception propagate; we only
    # care about cwd state at the call site and after.
    with (
        mock.patch("nanobrain.core.workflow.Workflow", fake_workflow_cls),
        contextlib.suppress(_StubFromConfig),
    ):
        ensure_dictionary(cfg)

    # 1. cwd was changed BEFORE Workflow.from_config was called
    assert cwd_at_workflow_call, "Workflow.from_config was never reached"
    drive_cwd = cwd_at_workflow_call[0]
    assert drive_cwd != prev_cwd, (
        f"_drive_workflow should chdir to a writable apecx state dir before "
        f"calling Workflow.from_config; cwd stayed at {drive_cwd!r}"
    )

    # 2. The chosen cwd is writable — the whole point of the fix
    assert os.access(drive_cwd, os.W_OK), f"chosen cwd is not writable: {drive_cwd}"

    # 3. cwd was restored after the exception, via the finally clause
    assert os.getcwd() == prev_cwd, (
        f"cwd not restored after exception; now at {os.getcwd()!r}, expected {prev_cwd!r}"
    )


def test_logs_clear_progress_message_at_build(
    isolated_paths: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """First-time build should emit a warning naming the wall-clock cost."""
    sqlite = isolated_paths / "out" / "dictionary.sqlite"
    data_root = isolated_paths / "data"
    (data_root / "violin").mkdir(parents=True)
    (data_root / "violin" / "Pathogen_Information.csv").write_text("name\nfoo\n")

    cfg = EnsureDictionaryConfig(sqlite_path=sqlite, data_root=data_root)

    async def fake_drive(_cfg):
        sqlite.parent.mkdir(parents=True, exist_ok=True)
        sqlite.touch()
        return sqlite

    caplog.set_level(logging.WARNING)
    with mock.patch(
        "apecx_integration.synonym_dictionary.workflow.bootstrap._drive_workflow",
        side_effect=fake_drive,
    ):
        ensure_dictionary(cfg)

    messages = " ".join(rec.message for rec in caplog.records)
    assert "10–15 minutes" in messages or "Building synonym dictionary" in messages
    assert "APECX_SKIP_DICT_BUILD" in messages
