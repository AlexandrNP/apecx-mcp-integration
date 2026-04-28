"""
Unit tests for apecx_integration.cli.setup_data.

Verifies: gh availability checks, download dispatch, extraction, and
the Claude Desktop config-update logic.  Does NOT shell out to GitHub
or gh.
"""

import json
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from apecx_integration.cli.setup_data import (
    _DEFAULT_LLM_ENV,
    _EXPECTED_FILES,
    _default_claude_config_path,
    _find_apecx_mcp_binary,
    _gh_authenticated,
    _gh_available,
    _update_claude_config,
    main,
)


# ---------------------------------------------------------------------------
# _gh_available / _gh_authenticated
# ---------------------------------------------------------------------------
def test_gh_available_when_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
    assert _gh_available() is True


def test_gh_available_when_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert _gh_available() is False


def test_gh_authenticated_success(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))
    assert _gh_authenticated() is True


def test_gh_authenticated_failure(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=1))
    assert _gh_authenticated() is False


# ---------------------------------------------------------------------------
# _default_claude_config_path
# ---------------------------------------------------------------------------
def test_default_claude_config_path_per_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    p = _default_claude_config_path()
    assert "Library/Application Support/Claude" in str(p)
    assert p.name == "claude_desktop_config.json"

    monkeypatch.setattr(sys, "platform", "linux")
    p = _default_claude_config_path()
    assert ".config/Claude" in str(p)


# ---------------------------------------------------------------------------
# _find_apecx_mcp_binary
# ---------------------------------------------------------------------------
def test_find_apecx_mcp_binary_via_path(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda c: "/usr/bin/apecx-mcp" if c == "apecx-mcp" else None
    )
    assert _find_apecx_mcp_binary() == "/usr/bin/apecx-mcp"


def test_find_apecx_mcp_binary_none_found(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert _find_apecx_mcp_binary() is None


# ---------------------------------------------------------------------------
# _update_claude_config — creates new config when missing
# ---------------------------------------------------------------------------
def test_update_creates_new_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._find_apecx_mcp_binary",
        lambda: "/fake/apecx-mcp",
    )
    config = tmp_path / "subdir" / "claude_desktop_config.json"
    data_dir = tmp_path / "data"

    change = _update_claude_config(config, data_dir)

    assert config.exists()
    assert "created new" in change
    parsed = json.loads(config.read_text())
    apecx = parsed["mcpServers"]["apecx"]
    assert apecx["command"] == "/fake/apecx-mcp"
    assert apecx["env"]["APECX_DATA_ROOT"] == str(data_dir)
    # Default LLM env vars are seeded for new entries.
    for key in _DEFAULT_LLM_ENV:
        assert apecx["env"][key] == _DEFAULT_LLM_ENV[key]


def test_update_creates_apecx_in_existing_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._find_apecx_mcp_binary",
        lambda: "/usr/bin/apecx-mcp",
    )
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    data_dir = tmp_path / "data"

    change = _update_claude_config(config, data_dir)

    parsed = json.loads(config.read_text())
    assert parsed["mcpServers"]["other"] == {"command": "x"}, "preserved unrelated server"
    assert parsed["mcpServers"]["apecx"]["env"]["APECX_DATA_ROOT"] == str(data_dir)
    assert "created new" in change


# ---------------------------------------------------------------------------
# _update_claude_config — preserves existing apecx block
# ---------------------------------------------------------------------------
def test_update_preserves_existing_apecx_env(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "apecx": {
                        "command": "/some/where/apecx-mcp",
                        "args": ["--flag"],
                        "env": {
                            "APECX_LLM_BASE_URL": "http://my-llm:1234/v1",
                            "APECX_LLM_MODEL": "custom",
                            "OTHER": "preserved",
                        },
                    }
                }
            }
        )
    )
    data_dir = tmp_path / "data"

    change = _update_claude_config(config, data_dir)

    parsed = json.loads(config.read_text())
    apecx = parsed["mcpServers"]["apecx"]
    assert apecx["command"] == "/some/where/apecx-mcp", "command not touched"
    assert apecx["args"] == ["--flag"], "args not touched"
    assert apecx["env"]["APECX_LLM_BASE_URL"] == "http://my-llm:1234/v1"
    assert apecx["env"]["APECX_LLM_MODEL"] == "custom"
    assert apecx["env"]["OTHER"] == "preserved"
    assert apecx["env"]["APECX_DATA_ROOT"] == str(data_dir)
    assert "added APECX_DATA_ROOT" in change


def test_update_replaces_existing_data_root(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "apecx": {
                        "command": "/x",
                        "env": {"APECX_DATA_ROOT": "/old/path"},
                    }
                }
            }
        )
    )
    data_dir = tmp_path / "new_data"

    change = _update_claude_config(config, data_dir)

    apecx = json.loads(config.read_text())["mcpServers"]["apecx"]
    assert apecx["env"]["APECX_DATA_ROOT"] == str(data_dir)
    assert "/old/path" in change and str(data_dir) in change


def test_update_idempotent_when_already_correct(tmp_path):
    data_dir = tmp_path / "data"
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "apecx": {
                        "command": "/x",
                        "env": {"APECX_DATA_ROOT": str(data_dir)},
                    }
                }
            }
        )
    )

    change = _update_claude_config(config, data_dir)
    assert "no change" in change


# ---------------------------------------------------------------------------
# _update_claude_config — error paths
# ---------------------------------------------------------------------------
def test_update_rejects_malformed_json(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{ this is not valid json")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        _update_claude_config(config, tmp_path / "data")


def test_update_rejects_non_object_mcpservers(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": "not an object"}))
    with pytest.raises(RuntimeError, match="non-object 'mcpServers'"):
        _update_claude_config(config, tmp_path / "data")


def test_update_rejects_when_no_apecx_mcp_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._find_apecx_mcp_binary",
        lambda: None,
    )
    with pytest.raises(RuntimeError, match="locate the apecx-mcp"):
        _update_claude_config(tmp_path / "config.json", tmp_path / "data")


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
# main() — happy path with config update
# ---------------------------------------------------------------------------
def test_main_happy_path_with_config_update(monkeypatch, tmp_path, capsys):
    # Build a real tarball with the expected file layout.
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
    config_path = tmp_path / "claude_desktop_config.json"

    # Pre-create the config so the "found at default location" branch fires.
    config_path.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config_path,
    )
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._find_apecx_mcp_binary",
        lambda: "/usr/bin/apecx-mcp",
    )

    # input() sequence: data dir → "Use this config? Y/n" → "Add this block? Y/n"
    inputs = iter([str(dest_dir), "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)

    main()

    out = capsys.readouterr().out
    assert "All 6 data files extracted successfully" in out
    assert "Wrote " + str(config_path) in out
    assert "first-time install" in out
    # Block preview was shown.
    assert "/usr/bin/apecx-mcp" in out
    assert "APECX_LLM_BASE_URL" in out

    parsed = json.loads(config_path.read_text())
    assert parsed["mcpServers"]["apecx"]["env"]["APECX_DATA_ROOT"] == str(dest_dir)


def test_main_first_install_decline_does_not_write(monkeypatch, tmp_path, capsys):
    """User says yes to using the config but no to adding the block."""
    archive_dir = tmp_path / "archive_src"
    archive_dir.mkdir()
    (archive_dir / "violin").mkdir()
    for f in _EXPECTED_FILES:
        (archive_dir / f).write_text("data\n")
    archive_path = tmp_path / "apecx-data.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:  # noqa: S202
        for f in _EXPECTED_FILES:
            tf.add(archive_dir / f, arcname=f)

    dest_dir = tmp_path / "data"
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(json.dumps({"mcpServers": {}}))

    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config_path,
    )
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._find_apecx_mcp_binary",
        lambda: "/usr/bin/apecx-mcp",
    )
    inputs = iter([str(dest_dir), "y", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)
    main()

    assert json.loads(config_path.read_text()) == {"mcpServers": {}}, "config untouched"


def test_main_update_existing_apecx_shows_change_only(monkeypatch, tmp_path, capsys):
    """Existing apecx block: prompt should show only the data-root change, not the full block."""
    archive_dir = tmp_path / "archive_src"
    archive_dir.mkdir()
    (archive_dir / "violin").mkdir()
    for f in _EXPECTED_FILES:
        (archive_dir / f).write_text("data\n")
    archive_path = tmp_path / "apecx-data.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:  # noqa: S202
        for f in _EXPECTED_FILES:
            tf.add(archive_dir / f, arcname=f)

    dest_dir = tmp_path / "data"
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "apecx": {
                        "command": "/already/installed/apecx-mcp",
                        "args": [],
                        "env": {"APECX_LLM_API_KEY": "real-secret"},
                    }
                }
            }
        )
    )

    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config_path,
    )
    inputs = iter([str(dest_dir), "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)
    main()

    out = capsys.readouterr().out
    assert "Existing 'apecx' MCP server found" in out
    assert "first-time install" not in out, "should NOT use first-install language"
    assert "All other fields" in out, "must reassure user other fields preserved"

    parsed = json.loads(config_path.read_text())
    apecx = parsed["mcpServers"]["apecx"]
    assert apecx["env"]["APECX_LLM_API_KEY"] == "real-secret", "secret preserved"
    assert apecx["env"]["APECX_DATA_ROOT"] == str(dest_dir)
    assert apecx["command"] == "/already/installed/apecx-mcp"


def test_main_update_idempotent_when_already_correct(monkeypatch, tmp_path, capsys):
    """Re-running setup with identical APECX_DATA_ROOT should not re-write the file."""
    archive_dir = tmp_path / "archive_src"
    archive_dir.mkdir()
    (archive_dir / "violin").mkdir()
    for f in _EXPECTED_FILES:
        (archive_dir / f).write_text("data\n")
    archive_path = tmp_path / "apecx-data.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:  # noqa: S202
        for f in _EXPECTED_FILES:
            tf.add(archive_dir / f, arcname=f)

    dest_dir = tmp_path / "data"
    config_path = tmp_path / "claude_desktop_config.json"
    original = {
        "mcpServers": {
            "apecx": {
                "command": "/x",
                "env": {"APECX_DATA_ROOT": str(dest_dir)},
            }
        }
    }
    config_path.write_text(json.dumps(original))

    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config_path,
    )
    inputs = iter([str(dest_dir), "y"])  # only 2 inputs: data + "Use this config?"
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)
    main()

    out = capsys.readouterr().out
    assert "already set" in out.lower() or "Nothing to do" in out
    assert json.loads(config_path.read_text()) == original


def test_main_skips_config_update_when_user_declines(monkeypatch, tmp_path, capsys):
    archive_dir = tmp_path / "archive_src"
    archive_dir.mkdir()
    (archive_dir / "violin").mkdir()
    for f in _EXPECTED_FILES:
        (archive_dir / f).write_text("data\n")
    archive_path = tmp_path / "apecx-data.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:  # noqa: S202
        for f in _EXPECTED_FILES:
            tf.add(archive_dir / f, arcname=f)

    dest_dir = tmp_path / "data"
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(json.dumps({"mcpServers": {}}))

    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config_path,
    )
    # Input sequence: data dir → "Use this config? [Y/n]" → "Alternate path:"
    inputs = iter([str(dest_dir), "n", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)

    main()

    # Config untouched.
    assert json.loads(config_path.read_text()) == {"mcpServers": {}}
    assert "Skipped" in capsys.readouterr().out


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
