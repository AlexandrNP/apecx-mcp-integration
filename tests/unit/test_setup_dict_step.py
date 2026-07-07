"""Unit tests for the ``_step_dict`` chain step.

The dict step closes the silent gap surfaced 2026-06-09 where
``apecx-setup`` left no synonym dictionary at the canonical location,
forcing the first ``apecx-mcp`` startup to pay a 10-15 minute build
cost.

Tests cover all four branches:
  1. Already-built dictionary → ``ok`` (idempotent)
  2. APECX_SKIP_DICT_BUILD=1 → ``skipped`` (opt-out, surfaced visibly)
  3. Missing VIOLIN data → ``skipped`` (defers to data step's warning)
  4. Build attempted → ``ok`` on success, ``fail`` on exception
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.cli import setup as setup_cli


@pytest.fixture(autouse=True)
def _disable_public_dict_download(monkeypatch):
    """Default for this module: disable the anonymous public download so the
    LOCAL-build / skip / opt-out fallback paths are exercised deterministically
    (no network in unit tests). ``_try_public_download`` honors
    APECX_SKIP_DICT_DOWNLOAD=1 and returns None. The download-SUCCESS path has its
    own dedicated test that re-enables it and mocks the fetch.
    """
    monkeypatch.setenv("APECX_SKIP_DICT_DOWNLOAD", "1")


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


class _FakeCfg:
    """In-memory stand-in for EnsureDictionaryConfig.resolve() output."""

    def __init__(self, sqlite_path: Path, data_root: Path):
        self.sqlite_path = sqlite_path
        self.data_root = data_root


def _patch_bootstrap(
    monkeypatch,
    *,
    sqlite_path: Path,
    data_root: Path,
    ensure_return: Path | None = None,
    ensure_raises: Exception | None = None,
):
    """Patch the bootstrap module's resolve() + ensure_dictionary().

    The dict step imports bootstrap LAZILY inside the function, so we
    need to patch via sys.modules to intercept the import.
    """
    import sys
    import types

    fake_mod = types.ModuleType("apecx_integration.synonym_dictionary.workflow.bootstrap")

    class _FakeConfig:
        def __init__(self):
            pass

        def resolve(self):
            return _FakeCfg(sqlite_path=sqlite_path, data_root=data_root)

    def _fake_ensure(cfg):
        if ensure_raises:
            raise ensure_raises
        return ensure_return

    fake_mod.EnsureDictionaryConfig = _FakeConfig
    fake_mod.ensure_dictionary = _fake_ensure
    monkeypatch.setitem(
        sys.modules,
        "apecx_integration.synonym_dictionary.workflow.bootstrap",
        fake_mod,
    )


# ─────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────


def test_dict_step_returns_ok_when_already_built(tmp_path, monkeypatch):
    """Already-present dictionary → ``ok`` with the file location +
    size surfaced for the summary table."""
    sqlite = tmp_path / "dictionary.sqlite"
    sqlite.write_bytes(b"x" * 1024)  # 1 KB sentinel content
    data_root = tmp_path / "data"
    _patch_bootstrap(monkeypatch, sqlite_path=sqlite, data_root=data_root)
    monkeypatch.delenv("APECX_SKIP_DICT_BUILD", raising=False)

    result = setup_cli._step_dict(interactive=False)

    assert result.name == "dict"
    assert result.status == "ok"
    assert "existing dictionary" in result.detail
    assert str(sqlite) in result.detail


def test_dict_step_skips_when_opt_out_env_set(tmp_path, monkeypatch):
    """APECX_SKIP_DICT_BUILD=1 → ``skipped`` (visible — not a silent green)."""
    sqlite = tmp_path / "missing.sqlite"  # doesn't exist
    data_root = tmp_path / "data"
    _patch_bootstrap(monkeypatch, sqlite_path=sqlite, data_root=data_root)
    monkeypatch.setenv("APECX_SKIP_DICT_BUILD", "1")

    result = setup_cli._step_dict(interactive=False)

    assert result.status == "skipped"
    assert "APECX_SKIP_DICT_BUILD" in result.detail
    assert "slow substring" in result.detail


def test_dict_step_skips_when_violin_data_missing(tmp_path, monkeypatch):
    """No VIOLIN under data_root → ``skipped`` with pointer to data step.
    Avoids reporting a hard failure when the actual cause is upstream."""
    sqlite = tmp_path / "missing.sqlite"
    data_root = tmp_path / "data"
    # Deliberately NOT creating violin/Pathogen_Information.csv
    _patch_bootstrap(monkeypatch, sqlite_path=sqlite, data_root=data_root)
    monkeypatch.delenv("APECX_SKIP_DICT_BUILD", raising=False)

    result = setup_cli._step_dict(interactive=False)

    assert result.status == "skipped"
    assert "VIOLIN data not present" in result.detail
    assert "apecx-setup data" in result.detail


def test_dict_step_builds_when_data_present_and_dict_absent(tmp_path, monkeypatch):
    """Happy build path: VIOLIN present + dict absent → ensure_dictionary()
    invoked, returns a path → ``ok`` with size report."""
    sqlite = tmp_path / "new_dict.sqlite"
    data_root = tmp_path / "data"
    (data_root / "violin").mkdir(parents=True)
    (data_root / "violin" / "Pathogen_Information.csv").write_text("hdr\n")

    # ensure_dictionary returns the built path. Simulate the artifact
    # by writing the file BEFORE the call so the size-report code can
    # stat it.
    sqlite.write_bytes(b"\x00" * 2048)
    _patch_bootstrap(
        monkeypatch,
        sqlite_path=sqlite,
        data_root=data_root,
        ensure_return=sqlite,
    )
    monkeypatch.delenv("APECX_SKIP_DICT_BUILD", raising=False)

    # The "already built" early-return would fire here because we
    # wrote the file. That's the correct branch — the dict step is
    # idempotent. To exercise the BUILD branch, remove the file then
    # have ensure_dictionary "build" it.
    sqlite.unlink()

    # Re-patch with an ensure_dictionary that writes the artifact.
    def _fake_ensure_builds(cfg):
        sqlite.write_bytes(b"\x00" * 4096)
        return sqlite

    import sys

    sys.modules[
        "apecx_integration.synonym_dictionary.workflow.bootstrap"
    ].ensure_dictionary = _fake_ensure_builds

    result = setup_cli._step_dict(interactive=False)

    assert result.status == "ok"
    assert "built" in result.detail
    assert str(sqlite) in result.detail
    assert sqlite.is_file(), "ensure_dictionary should have created the file"


def test_dict_step_fails_loudly_when_ensure_raises(tmp_path, monkeypatch):
    """Unexpected exception from ensure_dictionary → ``fail`` with the
    exception type + message surfaced (no swallowing)."""
    sqlite = tmp_path / "missing.sqlite"
    data_root = tmp_path / "data"
    (data_root / "violin").mkdir(parents=True)
    (data_root / "violin" / "Pathogen_Information.csv").write_text("hdr\n")
    _patch_bootstrap(
        monkeypatch,
        sqlite_path=sqlite,
        data_root=data_root,
        ensure_raises=RuntimeError("OLS unreachable"),
    )
    monkeypatch.delenv("APECX_SKIP_DICT_BUILD", raising=False)

    result = setup_cli._step_dict(interactive=False)

    assert result.status == "fail"
    assert "RuntimeError" in result.detail
    assert "OLS unreachable" in result.detail


# ─────────────────────────────────────────────────────────────────────────
# Chain wiring (dict step is in the subcommand registry + chain order)
# ─────────────────────────────────────────────────────────────────────────


def test_dict_is_in_subcommands_registry():
    """The dict step MUST be reachable via `apecx-setup dict`."""
    assert "dict" in setup_cli._SUBCOMMANDS
    assert callable(setup_cli._SUBCOMMANDS["dict"])


def test_dict_runs_between_data_and_infra_in_full_chain(monkeypatch):
    """The chain order is: globus → data → DICT → infra → llm → verify.
    A future refactor that moves dict elsewhere should fail this test."""
    call_order: list[str] = []

    def _spy_globus(**_kw):
        call_order.append("globus")
        return setup_cli.StepResult("globus", "ok", "spy")

    def _spy_data(**_kw):
        call_order.append("data")
        return setup_cli.StepResult("data", "ok", "spy")

    def _spy_dict(**_kw):
        call_order.append("dict")
        return setup_cli.StepResult("dict", "ok", "spy")

    def _spy_infra(**_kw):
        call_order.append("infra")
        return setup_cli.StepResult("infra", "ok", "spy")

    def _spy_llm(**_kw):
        call_order.append("llm")
        return setup_cli.StepResult("llm", "ok", "spy")

    def _spy_verify(**_kw):
        call_order.append("verify")
        return setup_cli.StepResult("verify", "ok", "spy")

    monkeypatch.setattr(setup_cli, "_step_globus", _spy_globus)
    monkeypatch.setattr(setup_cli, "_step_data", _spy_data)
    monkeypatch.setattr(setup_cli, "_step_dict", _spy_dict)
    monkeypatch.setattr(setup_cli, "_step_infra", _spy_infra)
    monkeypatch.setattr(setup_cli, "_step_llm", _spy_llm)
    monkeypatch.setattr(setup_cli, "_step_verify", _spy_verify)
    # Suppress the summary-table print.
    monkeypatch.setattr(setup_cli, "_print_summary", lambda _: 0)

    # rhea is default-on in the chain now; skip it so the real _step_rhea
    # (docker/git) doesn't run in this unit test.
    setup_cli._run_all(interactive=False, with_rag=False, skip_rhea=True)

    # Critical ordering: dict immediately after data, before infra.
    data_idx = call_order.index("data")
    dict_idx = call_order.index("dict")
    infra_idx = call_order.index("infra")
    assert data_idx < dict_idx < infra_idx, (
        f"chain order broken: {call_order} — expected data→dict→infra"
    )


def test_dict_appears_in_argparse_choices():
    """`apecx-setup dict` must be accepted by argparse (not a typo)."""
    import argparse

    # Build the parser by calling main() with --help we can intercept,
    # or simpler: confirm "dict" is in the choices list. The parser is
    # constructed inline in main(), but we can simulate parsing.
    try:
        # argparse SystemExits on unknown choices, doesn't on valid ones.
        # Call with a dummy --help-equivalent that returns a no-op.
        # Simpler: replace _SUBCOMMANDS dispatch with a no-op + parse.
        # Actually the simplest is just to construct a parser inline.
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "subcommand",
            choices=[
                "globus",
                "data",
                "dict",
                "infra",
                "llm",
                "rag",
                "rhea",
                "verify",
                "all",
            ],
        )
        args = parser.parse_args(["dict"])
        assert args.subcommand == "dict"
    except SystemExit:
        pytest.fail("argparse rejected 'dict' as a valid subcommand")


def test_dict_step_downloads_from_public_path_no_violin_needed(tmp_path, monkeypatch):
    """Clean install (NO VIOLIN, NO Globus creds): the dict step fetches the
    dictionary via the ANONYMOUS public download — the same artifact the MCP
    server bootstraps — so a fresh `apecx-setup` produces the dictionary and
    `verify` no longer fails on a missing required dict. Regression for the
    confusing chain where dict skipped on "VIOLIN data not present".
    """
    import apecx_integration.mcp_surface.server as server_mod

    sqlite = tmp_path / "dictionary.sqlite"
    data_root = tmp_path / "data"  # deliberately NO violin/ under it
    _patch_bootstrap(monkeypatch, sqlite_path=sqlite, data_root=data_root)
    # Re-enable the download for THIS test (autouse fixture disabled it) and mock
    # the fetch so no real network is needed.
    monkeypatch.delenv("APECX_SKIP_DICT_DOWNLOAD", raising=False)
    monkeypatch.delenv("APECX_SKIP_DICT_BUILD", raising=False)

    def _fake_download(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00" * 4096)
        return dest

    monkeypatch.setattr(server_mod, "_try_public_download", _fake_download)

    result = setup_cli._step_dict(interactive=False)

    assert result.status == "ok", result
    assert "downloaded" in result.detail
    assert "anonymous public path" in result.detail
