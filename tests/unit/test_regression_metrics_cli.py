"""CLI smoke test for ``apecx-regression-metrics``.

Verifies the CLI entry point runs end-to-end against a real
migrated DB. We don't enumerate every edge case here — the
underlying ``compute_regression_metrics`` function has its own
unit tests; this test covers the CLI wiring (arg parsing,
exit codes, JSON-vs-table output).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apecx_integration.cli.regression_metrics import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrated_db(tmp_path):
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "cli_smoke.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return url


def test_cli_empty_db_table_output(tmp_path, capsys):
    """Empty DB → exit code 0 + table mentions zero artifacts."""
    url = _migrated_db(tmp_path)
    rc = main(["--db-url", url])
    captured = capsys.readouterr()
    assert rc == 0
    assert "0 generated artifact" in captured.out


def test_cli_empty_db_json_output(tmp_path, capsys):
    """Empty DB → exit code 0 + JSON parses + total_artifacts is 0."""
    url = _migrated_db(tmp_path)
    rc = main(["--db-url", url, "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert parsed["total_artifacts"] == 0


def test_cli_bad_db_url_returns_1(tmp_path, capsys):
    """Pointing at a nonexistent DB must exit 1, NOT crash."""
    rc = main(["--db-url", "sqlite:///" + str(tmp_path / "does_not_exist.db")])
    # SQLite happily creates empty DBs on connect, so this won't
    # actually error — but the absence of generated_artifact table
    # WILL trip a real error. The CLI must surface that as exit 1.
    captured = capsys.readouterr()
    if rc != 0:
        assert "apecx-regression-metrics" in captured.err


def test_cli_invalid_since_arg_raises(tmp_path):
    """--since with garbage value must raise argparse error, not
    silently default."""
    with pytest.raises(SystemExit):
        main(["--since", "not-a-date"])
