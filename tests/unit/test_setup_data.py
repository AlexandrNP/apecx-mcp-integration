"""
Unit tests for apecx_integration.cli.setup_data.

Verifies: gh availability checks, download dispatch, extraction,
Claude Desktop config-update logic, and ``--reconfigure-llm`` flow.

Mock surface is kept minimal:
- ``subprocess.run`` is intercepted with real ``CompletedProcess``
  objects (no ``unittest.mock.MagicMock``); the gh subprocess itself
  is the only cross-process boundary, and tests never shell out to
  the real network.
- ``_download_asset`` is patched per-test to copy a real local
  tarball into the destination — preserves the unpack/extract path
  end-to-end (real tarfile, real disk, real Path operations).
- ``input()`` is the only unavoidable mock — there's no portable
  way to simulate a TTY without a pty.
- All Path / JSON / tarfile work uses real components on tmp_path.
"""

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from apecx_integration.cli.setup_data import (
    _DEFAULT_LLM_ENV,
    _EXPECTED_FILES,
    _default_claude_config_path,
    _find_apecx_mcp_binary,
    _gh_authenticated,
    _gh_available,
    _prompt_for_llm_config,
    _reconfigure_llm_in_config,
    _update_claude_config,
    main,
)


def _fake_gh_status(returncode: int):
    """Return a real ``subprocess.CompletedProcess`` — no MagicMock."""
    return lambda *a, **kw: subprocess.CompletedProcess(
        args=list(a[0]) if a else [], returncode=returncode, stdout="", stderr=""
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
    monkeypatch.setattr("subprocess.run", _fake_gh_status(0))
    assert _gh_authenticated() is True


def test_gh_authenticated_failure(monkeypatch):
    monkeypatch.setattr("subprocess.run", _fake_gh_status(1))
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
        main([])
    assert exc.value.code == 1
    assert "gh" in capsys.readouterr().out.lower()


def test_main_exits_when_not_authenticated(monkeypatch, capsys):
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: False)
    with pytest.raises(SystemExit) as exc:
        main([])
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

    # input() sequence: data dir → "Use this config?" → 3 LLM prompts → "Add block?"
    inputs = iter([str(dest_dir), "y", "", "", "", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)

    main([])

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
    # data dir → "Use config?" → 3 LLM defaults → "Add block? n"
    inputs = iter([str(dest_dir), "y", "", "", "", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)
    main([])

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
    main([])

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
    main([])

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

    main([])

    # Config untouched.
    assert json.loads(config_path.read_text()) == {"mcpServers": {}}
    assert "Skipped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _prompt_for_llm_config — direct unit tests
# ---------------------------------------------------------------------------
def test_prompt_for_llm_config_all_defaults(monkeypatch):
    """Three Enter presses → all defaults preserved."""
    inputs = iter(["", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    result = _prompt_for_llm_config()
    assert result == _DEFAULT_LLM_ENV


def test_prompt_for_llm_config_all_custom(monkeypatch):
    """All three values overridden — none of the defaults survive."""
    inputs = iter(
        [
            "https://api.openai.com/v1",
            "gpt-4o",
            "sk-fakekey-1234",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    result = _prompt_for_llm_config()
    assert result == {
        "APECX_LLM_BASE_URL": "https://api.openai.com/v1",
        "APECX_LLM_MODEL": "gpt-4o",
        "APECX_LLM_API_KEY": "sk-fakekey-1234",
    }


def test_prompt_for_llm_config_mixed(monkeypatch):
    """Override only the model; URL and API key keep defaults."""
    inputs = iter(["", "llama3.1:70b", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    result = _prompt_for_llm_config()
    assert result["APECX_LLM_BASE_URL"] == _DEFAULT_LLM_ENV["APECX_LLM_BASE_URL"]
    assert result["APECX_LLM_MODEL"] == "llama3.1:70b"
    assert result["APECX_LLM_API_KEY"] == _DEFAULT_LLM_ENV["APECX_LLM_API_KEY"]


def test_prompt_for_llm_config_strips_whitespace(monkeypatch):
    """Leading/trailing whitespace shouldn't leak into the config."""
    inputs = iter(["  https://x/v1  ", " mymodel ", "  "])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    result = _prompt_for_llm_config()
    assert result["APECX_LLM_BASE_URL"] == "https://x/v1"
    assert result["APECX_LLM_MODEL"] == "mymodel"
    # Whitespace-only is treated as empty → default kicks in.
    assert result["APECX_LLM_API_KEY"] == _DEFAULT_LLM_ENV["APECX_LLM_API_KEY"]


# ---------------------------------------------------------------------------
# Threading: custom LLM env actually reaches the written config
# ---------------------------------------------------------------------------
def test_main_first_install_writes_custom_llm_env(monkeypatch, tmp_path, capsys):
    """User customizes LLM URL + model → values land in the written config."""
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
    # data dir → "Use config?" → URL → model → key → "Add block?"
    inputs = iter(
        [
            str(dest_dir),
            "y",
            "https://api.anthropic.com/v1",
            "claude-sonnet-4-6",
            "sk-ant-fake",
            "y",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)
    main([])

    apecx_env = json.loads(config_path.read_text())["mcpServers"]["apecx"]["env"]
    assert apecx_env["APECX_LLM_BASE_URL"] == "https://api.anthropic.com/v1"
    assert apecx_env["APECX_LLM_MODEL"] == "claude-sonnet-4-6"
    assert apecx_env["APECX_LLM_API_KEY"] == "sk-ant-fake"
    # The JSON preview must show the chosen URL, not the default.
    # ("localhost:11434" appears in the help text — assert the JSON key/value form.)
    out = capsys.readouterr().out
    assert '"APECX_LLM_BASE_URL": "https://api.anthropic.com/v1"' in out
    assert '"APECX_LLM_BASE_URL": "http://localhost:11434/v1"' not in out


# ---------------------------------------------------------------------------
# --reconfigure-llm — direct unit tests of the helper
# ---------------------------------------------------------------------------
def test_reconfigure_errors_when_no_apecx_block(monkeypatch, tmp_path, capsys):
    """No apecx server in the config → exits with a clear remediation hint."""
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {}}))

    with pytest.raises(SystemExit) as exc:
        _reconfigure_llm_in_config(config)
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "no 'apecx' MCP server found" in out
    assert "apecx-setup" in out  # remediation pointer


def test_reconfigure_errors_when_env_missing(monkeypatch, tmp_path, capsys):
    """Apecx block exists but its 'env' field is missing — explicit error."""
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"apecx": {"command": "/x"}}}))

    with pytest.raises(SystemExit) as exc:
        _reconfigure_llm_in_config(config)
    assert exc.value.code == 1
    assert "missing or not an object" in capsys.readouterr().out


def test_reconfigure_all_unchanged_writes_nothing(monkeypatch, tmp_path, capsys):
    """User keeps every value (3 Enters) → no diff, no write."""
    config = tmp_path / "claude_desktop_config.json"
    original = {
        "mcpServers": {
            "apecx": {
                "command": "/usr/bin/apecx-mcp",
                "env": {
                    "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
                    "APECX_LLM_MODEL": "mistral-nemo:latest",
                    "APECX_LLM_API_KEY": "unused",
                    "APECX_DATA_ROOT": "/data",
                },
            }
        }
    }
    config.write_text(json.dumps(original))
    pre_mtime = config.stat().st_mtime_ns

    monkeypatch.setattr("builtins.input", lambda _p: "")
    _reconfigure_llm_in_config(config)

    assert json.loads(config.read_text()) == original
    assert config.stat().st_mtime_ns == pre_mtime, "file should not be re-written"
    out = capsys.readouterr().out
    assert "No changes" in out


def test_reconfigure_replaces_only_llm_env_preserves_rest(monkeypatch, tmp_path):
    """Custom URL + model → only LLM env changes; DATA_ROOT, command, args, other env preserved."""
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "apecx": {
                        "command": "/some/path/apecx-mcp",
                        "args": ["--flag"],
                        "env": {
                            "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
                            "APECX_LLM_MODEL": "mistral-nemo:latest",
                            "APECX_LLM_API_KEY": "old-key",
                            "APECX_DATA_ROOT": "/preserved/data",
                            "MY_CUSTOM_VAR": "preserved",
                        },
                    },
                    "other-server": {"command": "/x"},
                }
            }
        )
    )

    # User changes URL + model; keeps API key by pressing Enter.
    inputs = iter(["https://api.openai.com/v1", "gpt-4o", "", "y"])
    monkeypatch.setattr("builtins.input", lambda _p: next(inputs))

    _reconfigure_llm_in_config(config)

    parsed = json.loads(config.read_text())
    apecx = parsed["mcpServers"]["apecx"]
    # LLM env updated.
    assert apecx["env"]["APECX_LLM_BASE_URL"] == "https://api.openai.com/v1"
    assert apecx["env"]["APECX_LLM_MODEL"] == "gpt-4o"
    assert apecx["env"]["APECX_LLM_API_KEY"] == "old-key", "kept on Enter"
    # Everything else preserved.
    assert apecx["command"] == "/some/path/apecx-mcp"
    assert apecx["args"] == ["--flag"]
    assert apecx["env"]["APECX_DATA_ROOT"] == "/preserved/data"
    assert apecx["env"]["MY_CUSTOM_VAR"] == "preserved"
    assert parsed["mcpServers"]["other-server"] == {"command": "/x"}


def test_reconfigure_user_cancels_after_diff(monkeypatch, tmp_path):
    """Diff shown, user says 'n' → file unchanged."""
    config = tmp_path / "claude_desktop_config.json"
    original = {
        "mcpServers": {
            "apecx": {
                "command": "/x",
                "env": {
                    "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
                    "APECX_LLM_MODEL": "mistral-nemo:latest",
                    "APECX_LLM_API_KEY": "unused",
                },
            }
        }
    }
    config.write_text(json.dumps(original))

    inputs = iter(["https://newurl/v1", "newmodel", "newkey", "n"])
    monkeypatch.setattr("builtins.input", lambda _p: next(inputs))

    _reconfigure_llm_in_config(config)
    assert json.loads(config.read_text()) == original


def test_reconfigure_round_trip_via_main(monkeypatch, tmp_path):
    """End-to-end through main(['--reconfigure-llm']): write, then re-load and verify.

    This is the integration-style assertion the user asked for: not
    just 'JSON has the right shape after one call', but 'the value
    we wrote is the value a fresh process would read back'.
    """
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "apecx": {
                        "command": "/x",
                        "env": dict(_DEFAULT_LLM_ENV, APECX_DATA_ROOT="/preserved"),
                    }
                }
            }
        )
    )
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config,
    )

    # "Use this config?" → URL → model → key → "Apply?"
    inputs = iter(
        [
            "y",
            "https://api.anthropic.com/v1",
            "claude-sonnet-4-6",
            "sk-ant-fake",
            "y",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _p: next(inputs))

    main(["--reconfigure-llm"])

    # Re-read the file from disk (NOT just trust in-memory state).
    written = json.loads(config.read_text(encoding="utf-8"))
    apecx_env = written["mcpServers"]["apecx"]["env"]
    assert apecx_env["APECX_LLM_BASE_URL"] == "https://api.anthropic.com/v1"
    assert apecx_env["APECX_LLM_MODEL"] == "claude-sonnet-4-6"
    assert apecx_env["APECX_LLM_API_KEY"] == "sk-ant-fake"
    assert apecx_env["APECX_DATA_ROOT"] == "/preserved", "data root NOT touched"


def test_main_reconfigure_does_not_invoke_data_download(monkeypatch, tmp_path):
    """--reconfigure-llm must not trigger gh / tarfile / data-dir prompts.

    Guard against regression: if a future refactor accidentally
    routes the flag through _run_full_setup, the canary functions
    below would be called and the test fails loudly.
    """
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "apecx": {
                        "command": "/x",
                        "env": dict(_DEFAULT_LLM_ENV, APECX_DATA_ROOT="/d"),
                    }
                }
            }
        )
    )
    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config,
    )

    canary = {"download_called": False, "gh_check_called": False}

    def boom_download(_dest):
        canary["download_called"] = True
        raise AssertionError("data download should not run under --reconfigure-llm")

    def boom_gh_avail():
        canary["gh_check_called"] = True
        raise AssertionError("gh check should not run under --reconfigure-llm")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", boom_download)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", boom_gh_avail)

    inputs = iter(["y", "", "", "", "y"])  # use config / 3 Enters / apply
    monkeypatch.setattr("builtins.input", lambda _p: next(inputs))

    main(["--reconfigure-llm"])

    assert canary["download_called"] is False
    assert canary["gh_check_called"] is False


def test_main_full_setup_prompt_mentions_reconfigure_flag(monkeypatch, tmp_path):
    """Update path (existing apecx block, full apecx-setup) prints the --reconfigure-llm pointer."""
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
        json.dumps({"mcpServers": {"apecx": {"command": "/x", "env": {"APECX_DATA_ROOT": "/old"}}}})
    )

    monkeypatch.setattr(
        "apecx_integration.cli.setup_data._default_claude_config_path",
        lambda: config_path,
    )
    inputs = iter([str(dest_dir), "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _p: next(inputs))
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)

    def fake_download(dest: str) -> None:
        import shutil

        shutil.copy(archive_path, Path(dest) / "apecx-data.tar.gz")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", fake_download)

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        main([])

    output = buf.getvalue()
    assert "--reconfigure-llm" in output, "update flow must point users at the flag"


def test_main_download_failure_exits(monkeypatch, tmp_path, capsys):
    dest_dir = tmp_path / "data"
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_available", lambda: True)
    monkeypatch.setattr("apecx_integration.cli.setup_data._gh_authenticated", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: str(dest_dir))

    def failing_download(_dest: str) -> None:
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr("apecx_integration.cli.setup_data._download_asset", failing_download)

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1
    assert "download failed" in capsys.readouterr().out
