"""
Unit tests for apecx_integration.cli.setup_data.

Data acquisition is now Globus-only — the legacy ``gh release download``
path was retired 2026-05-21, so this module no longer downloads anything.
It provides the operator-facing helpers the Globus path reuses
(``prompt_for_data_dir``, ``report_post_transfer_layout``), the Claude
Desktop config-update logic, and the ``--reconfigure-llm`` flow.

Mock surface is kept minimal:
- ``input()`` is the only unavoidable mock — there's no portable
  way to simulate a TTY without a pty.
- All Path / JSON work uses real components on tmp_path. No test
  shells out or hits the network.
"""

import json
import sys

import pytest

from apecx_integration.cli.setup_data import (
    _DEFAULT_DATA_DIR,
    _DEFAULT_LLM_ENV,
    _EXPECTED_FILES,
    _default_claude_config_path,
    _find_apecx_mcp_binary,
    _prompt_for_llm_config,
    _reconfigure_llm_in_config,
    _update_claude_config,
    main,
    prompt_for_data_dir,
    report_post_transfer_layout,
)


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
# prompt_for_data_dir — Globus path's data-dir chooser + overwrite guard
# ---------------------------------------------------------------------------
def test_prompt_for_data_dir_empty_input_returns_default(monkeypatch, tmp_path):
    """Pressing Enter at the prompt picks ``_DEFAULT_DATA_DIR``.

    The module's real ``_DEFAULT_DATA_DIR`` (``~/.apecx/data``) may already
    exist with CSVs on the developer's machine, which would trip the
    overwrite guard. Repoint it at a non-existent tmp_path location so the
    test asserts the empty-input branch in isolation.
    """
    fake_default = tmp_path / "default_data"
    monkeypatch.setattr("apecx_integration.cli.setup_data._DEFAULT_DATA_DIR", fake_default)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert prompt_for_data_dir() == fake_default


def test_prompt_for_data_dir_explicit_path(monkeypatch, tmp_path):
    """A typed path (that does not yet exist) is returned verbatim."""
    target = tmp_path / "chosen"
    monkeypatch.setattr("builtins.input", lambda _prompt: str(target))
    assert prompt_for_data_dir() == target


def test_prompt_for_data_dir_non_interactive_skips_input(monkeypatch):
    """interactive=False returns the default WITHOUT calling input()."""

    def boom(_prompt):
        raise AssertionError("input() must not be called in non-interactive mode")

    monkeypatch.setattr("builtins.input", boom)
    assert prompt_for_data_dir(interactive=False) == _DEFAULT_DATA_DIR


def test_prompt_for_data_dir_existing_csv_abort(monkeypatch, tmp_path, capsys):
    """Existing dir with a CSV + answer 'n' → returns None and prints 'Aborted.'."""
    data_dir = tmp_path / "existing"
    data_dir.mkdir()
    (data_dir / "old.csv").write_text("a,b\n")

    inputs = iter([str(data_dir), "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    assert prompt_for_data_dir() is None
    assert "Aborted." in capsys.readouterr().out


def test_prompt_for_data_dir_existing_csv_overwrite(monkeypatch, tmp_path):
    """Existing dir with a CSV + answer 'y' → returns the chosen dir."""
    data_dir = tmp_path / "existing"
    data_dir.mkdir()
    (data_dir / "old.csv").write_text("a,b\n")

    inputs = iter([str(data_dir), "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    assert prompt_for_data_dir() == data_dir


def test_prompt_for_data_dir_existing_dir_without_csv_no_overwrite_prompt(monkeypatch, tmp_path):
    """Existing dir with no CSVs → returned directly, no overwrite prompt."""
    data_dir = tmp_path / "empty_existing"
    data_dir.mkdir()
    (data_dir / "readme.txt").write_text("not a csv\n")

    # Only one input() call expected (the data-dir prompt); a second call
    # would StopIteration and fail the test, proving no overwrite prompt.
    inputs = iter([str(data_dir)])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    assert prompt_for_data_dir() == data_dir


# ---------------------------------------------------------------------------
# report_post_transfer_layout — Globus completion summary
# ---------------------------------------------------------------------------
def test_report_post_transfer_layout_all_present(tmp_path, capsys):
    """All expected files present → returns [] and prints the all-present line."""
    for f in _EXPECTED_FILES:
        path = tmp_path / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data\n")

    missing = report_post_transfer_layout(tmp_path)

    assert missing == []
    out = capsys.readouterr().out
    assert f"All {len(_EXPECTED_FILES)} data files present under {tmp_path}." in out


def test_report_post_transfer_layout_some_missing(tmp_path, capsys):
    """Some files missing → returns the missing list and prints a WARNING."""
    present = _EXPECTED_FILES[:2]
    expected_missing = _EXPECTED_FILES[2:]
    for f in present:
        path = tmp_path / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data\n")

    missing = report_post_transfer_layout(tmp_path)

    assert missing == expected_missing
    out = capsys.readouterr().out
    assert f"WARNING: {len(expected_missing)} expected file(s) not found under {tmp_path}:" in out
    for f in expected_missing:
        assert str(tmp_path / f) in out


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
    """--reconfigure-llm must not trigger the data-dir prompt.

    Guard against regression: if a future refactor accidentally routes
    the flag through the data-acquisition path, ``prompt_for_data_dir``
    would be called and this test fails loudly. (The gh download path
    was retired 2026-05-21; data acquisition is Globus-only and lives
    in ``cli/setup.py``, not here.)
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

    def boom_prompt(*_a, **_kw):
        raise AssertionError("prompt_for_data_dir must not run under --reconfigure-llm")

    monkeypatch.setattr("apecx_integration.cli.setup_data.prompt_for_data_dir", boom_prompt)

    inputs = iter(["y", "", "", "", "y"])  # use config / 3 Enters / apply
    monkeypatch.setattr("builtins.input", lambda _p: next(inputs))

    main(["--reconfigure-llm"])


def test_main_default_branch_points_at_apecx_setup_without_downloading(monkeypatch, capsys):
    """Plain ``main([])`` only prints a pointer to apecx-setup; it downloads nothing."""

    def boom_prompt(*_a, **_kw):
        raise AssertionError("the default branch must not prompt for a data dir")

    monkeypatch.setattr("apecx_integration.cli.setup_data.prompt_for_data_dir", boom_prompt)

    def boom_input(_prompt):
        raise AssertionError("the default branch must not prompt for input")

    monkeypatch.setattr("builtins.input", boom_input)

    main([])

    out = capsys.readouterr().out
    assert "apecx-setup" in out
    assert "Globus" in out
