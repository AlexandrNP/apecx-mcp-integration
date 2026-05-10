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


def test_drive_workflow_does_not_chdir_post_g33(
    isolated_paths: Path,
) -> None:
    """G33 (2026-05-09) — pin that bootstrap does NOT chdir.

    Pre-G33 ``_drive_workflow`` did ``os.chdir(~/.apecx)`` before
    ``Workflow.from_config`` to work around nanobrain's
    ``Path("logs")`` cwd-relative log default. That default is gone:
    ``async_logging._default_writable_log_dir`` and
    ``logging_system._default_writable_log_dir`` now resolve to
    ``$NANOBRAIN_LOG_DIR`` -> ``~/.cache/nanobrain/logs/`` -> tempdir.

    The chdir hack was a *cwd-changing side effect* in a library
    function — itself a silent-failure source for any caller that
    expects relative paths to resolve against their original cwd.
    G33 retires it.

    This test pins the post-G33 contract: bootstrap leaves cwd
    untouched. If somebody re-introduces an os.chdir to "be safe,"
    this test fires.

    Source: ``eval_03_nanobrain_gap_inventory.md`` Round 4 G33;
    ``apecx-mcp-integration/docs/development_roadmap.md`` 8.6.
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

    with (
        mock.patch("nanobrain.core.workflow.Workflow", fake_workflow_cls),
        contextlib.suppress(_StubFromConfig),
    ):
        ensure_dictionary(cfg)

    # 1. Workflow.from_config was reached.
    assert cwd_at_workflow_call, "Workflow.from_config was never reached"
    drive_cwd = cwd_at_workflow_call[0]

    # 2. Post-G33 contract: cwd at Workflow.from_config call time MUST
    #    equal the caller's cwd. The framework's
    #    _default_writable_log_dir handles read-only-cwd scenarios on
    #    its own, anchored to $HOME/.cache or $TMPDIR — no chdir hack
    #    needed in the bootstrap layer.
    assert drive_cwd == prev_cwd, (
        f"bootstrap chdir'd to {drive_cwd!r} before "
        f"Workflow.from_config; G33 retired the chdir workaround. "
        f"If the framework regressed to cwd-relative log defaults, "
        f"fix _default_writable_log_dir, not the bootstrap layer."
    )

    # 3. cwd is unchanged after bootstrap (it never changed at all,
    #    but the assertion remains as a regression-detector).
    assert os.getcwd() == prev_cwd, (
        f"cwd changed across bootstrap call; now at {os.getcwd()!r}, was {prev_cwd!r}"
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
