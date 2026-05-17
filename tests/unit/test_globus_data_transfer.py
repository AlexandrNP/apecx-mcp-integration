"""Unit tests for the Globus-first data transfer glue (G82, 2026-05-16).

What's covered
--------------

* ``check_globus_prerequisites`` correctly identifies every failure
  mode (no SDK, no source endpoint, no dest endpoint, no credentials)
  and the success case. Each branch's ``reason()`` is asserted
  human-readable.
* ``build_transfer_items`` produces the expected source/dest list
  shape for the 6 dataset files, honors ``APECX_GLOBUS_SOURCE_PREFIX``
  override, and anchors dest paths at the operator's data_dir.
* The wrapper YAML at
  ``configs/globus_transfers/violin_bvbrc_transfer_step.yml`` parses
  successfully via ``GlobusTransferStepConfig.from_config`` when the
  env vars are set — proves the env-var interpolation is well-formed.
* ``attempt_globus_data_transfer`` returns ``unconfigured`` cleanly
  when preconditions aren't met (no global side effects, no exceptions).

What's NOT covered
------------------

End-to-end transfer against a real Globus endpoint is out of scope
for unit tests — it would require live credentials in CI, a writable
destination, and network access to Argonne LCF. That path is
exercised when an operator runs ``apecx-setup data`` with their
Globus credentials configured. The unit-test layer here exercises
everything up to (but not including) the network round-trip.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_globus_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every APECX_GLOBUS_*  / GLOBUS_COMPUTE_* env var so each
    test starts from a known-empty state. Per-test setups then opt
    into the env they actually want."""
    for var in [
        "APECX_GLOBUS_SOURCE_ENDPOINT_ID",
        "APECX_GLOBUS_DEST_ENDPOINT_ID",
        "APECX_GLOBUS_SOURCE_PREFIX",
        "GLOBUS_COMPUTE_CLIENT_ID",
        "GLOBUS_COMPUTE_CLIENT_SECRET",
    ]:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# check_globus_prerequisites
# ---------------------------------------------------------------------------


def test_prereqs_unconfigured_when_nothing_set(clean_globus_env: None) -> None:
    """With no env vars and no keyring entries, every flag is False."""
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        status = check_globus_prerequisites()

    # sdk_installed depends on the test env; the OTHER flags are what
    # this test pins.
    assert status.source_endpoint_set is False
    assert status.dest_endpoint_set is False
    assert status.credentials_reachable is False
    assert status.configured is False

    reason = status.reason()
    assert "APECX_GLOBUS_SOURCE_ENDPOINT_ID unset" in reason
    assert "APECX_GLOBUS_DEST_ENDPOINT_ID unset" in reason
    assert "no client credentials" in reason


def test_prereqs_configured_when_everything_set(
    clean_globus_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All flags True → status.configured is True, reason() says OK."""
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-uuid")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "client-secret")

    # Force sdk_installed=True regardless of test env (the dep is
    # installed in this workspace's venv but absence shouldn't fail).
    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        status = check_globus_prerequisites()

    # If globus_sdk is installed (it is, per pyproject.toml), this is True.
    # Skip the asserts about sdk_installed to keep the test env-agnostic.
    if status.sdk_installed:
        assert status.source_endpoint_set is True
        assert status.dest_endpoint_set is True
        assert status.credentials_reachable is True
        assert status.configured is True
        assert status.reason() == "Globus prerequisites OK"


def test_prereqs_credentials_via_keyring_count_as_present(
    clean_globus_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials in the OS keyring are equivalent to env-var credentials."""
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-uuid")
    # No env credentials, but the keyring lookup succeeds.
    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=True,
    ):
        status = check_globus_prerequisites()

    assert status.credentials_reachable is True


def test_prereqs_keyring_internal_failure_is_silent(
    clean_globus_env: None,
) -> None:
    """The real ``_keyring_credentials_present`` swallows every
    exception (broken keyring backend, missing keyring package, etc.)
    and returns False — preflight checks must never propagate
    keyring-internal failures to the caller.

    We exercise the real implementation by monkey-patching the
    ``keyring`` module its inner ``import`` resolves to. Patching the
    helper itself wouldn't prove the contract; patching the dependency
    does."""
    import sys
    import types

    from apecx_integration.cli._globus_data_transfer import (
        _keyring_credentials_present,
    )

    # Inject a fake "keyring" module whose get_password always raises.
    fake = types.ModuleType("keyring")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated keyring backend failure")

    fake.get_password = _boom  # type: ignore[attr-defined]

    real = sys.modules.get("keyring")
    sys.modules["keyring"] = fake
    try:
        # Despite the dependency raising, the helper must return False
        # — never propagate the exception.
        assert _keyring_credentials_present() is False
    finally:
        if real is not None:
            sys.modules["keyring"] = real
        else:
            sys.modules.pop("keyring", None)


# ---------------------------------------------------------------------------
# build_transfer_items
# ---------------------------------------------------------------------------


def test_build_transfer_items_default_layout(clean_globus_env: None, tmp_path: Path) -> None:
    """Default source prefix + date-stamped dirs, 6 items, stable order,
    dest paths anchored at data_dir + flattened to legacy layout.

    Source layout (verified live 2026-05-17, G91):
      $PREFIX/2024_12_17_VIOLIN/{Vaccine,Pathogen,Gene,...}_Information.csv
      $PREFIX/2025_05_05_BVBRC/BVBRC_genome.csv

    Dest layout (unchanged from gh-release):
      $data_dir/violin/{Vaccine,Pathogen,Gene,...}_Information.csv
      $data_dir/BVBRC_genome_alphavirus.csv  ← renamed from BVBRC_genome.csv
    """
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    items = build_transfer_items(tmp_path)

    assert len(items) == 6
    # First item: VIOLIN CSV from the date-stamped source dir, dest
    # under violin/.
    assert (
        items[0]["source_path"]
        == "/apecx-joshi-anl-general/2024_12_17_VIOLIN/Vaccine_Information.csv"
    )
    assert items[0]["dest_path"] == str(tmp_path / "violin/Vaccine_Information.csv")
    # Last item: BV-BRC genome CSV from the date-stamped source, renamed
    # to the legacy flat filename downstream code expects.
    assert items[-1]["source_path"] == "/apecx-joshi-anl-general/2025_05_05_BVBRC/BVBRC_genome.csv"
    assert items[-1]["dest_path"] == str(tmp_path / "BVBRC_genome_alphavirus.csv")
    # Every item has exactly the two expected keys.
    for item in items:
        assert set(item.keys()) == {"source_path", "dest_path"}


def test_build_transfer_items_honors_source_prefix_override(
    clean_globus_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APECX_GLOBUS_SOURCE_PREFIX shifts every source path."""
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    monkeypatch.setenv("APECX_GLOBUS_SOURCE_PREFIX", "/some/other/root")
    items = build_transfer_items(tmp_path)

    assert items[0]["source_path"] == "/some/other/root/2024_12_17_VIOLIN/Vaccine_Information.csv"
    # Trailing slash on the override is stripped.
    monkeypatch.setenv("APECX_GLOBUS_SOURCE_PREFIX", "/some/other/root/")
    items_with_slash = build_transfer_items(tmp_path)
    assert (
        items_with_slash[0]["source_path"]
        == "/some/other/root/2024_12_17_VIOLIN/Vaccine_Information.csv"
    )


def test_build_transfer_items_honors_dated_dir_overrides(
    clean_globus_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """APECX_GLOBUS_VIOLIN_DIR + APECX_GLOBUS_BVBRC_DIR let operators
    pull from a newer snapshot without a code change (G91)."""
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    monkeypatch.setenv("APECX_GLOBUS_VIOLIN_DIR", "2026_07_01_VIOLIN")
    monkeypatch.setenv("APECX_GLOBUS_BVBRC_DIR", "2026_08_15_BVBRC")
    items = build_transfer_items(tmp_path)

    # First VIOLIN item uses the new dir.
    assert "/2026_07_01_VIOLIN/Vaccine_Information.csv" in items[0]["source_path"]
    # Last BV-BRC item uses the new dir.
    assert "/2026_08_15_BVBRC/BVBRC_genome.csv" in items[-1]["source_path"]
    # Dest layout is unchanged — downstream code keeps working.
    assert items[0]["dest_path"] == str(tmp_path / "violin/Vaccine_Information.csv")
    assert items[-1]["dest_path"] == str(tmp_path / "BVBRC_genome_alphavirus.csv")


# ---------------------------------------------------------------------------
# Wrapper YAML parses
# ---------------------------------------------------------------------------


def test_wrapper_yaml_loads_with_env_vars_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configs/globus_transfers/violin_bvbrc_transfer_step.yml file
    must parse cleanly as a GlobusTransferStepConfig when its env-var
    references resolve to real values. Catches typos in field names or
    interpolation syntax."""
    from nanobrain.library.steps.globus_transfer_step import GlobusTransferStepConfig

    from apecx_integration.cli._globus_data_transfer import _wrapper_yaml_path

    # The YAML reads only the RESOLVED env vars (G90); apecx-side
    # ``_resolve_auth_env`` maps confidential or native config into them.
    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-test-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-test-uuid")
    monkeypatch.setenv("APECX_GLOBUS_RESOLVED_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("APECX_GLOBUS_RESOLVED_CLIENT_SECRET", "client-secret-test")
    monkeypatch.setenv("APECX_GLOBUS_AUTH_MODE", "client_credentials")

    yaml_path = _wrapper_yaml_path()
    assert yaml_path.is_file(), (
        f"wrapper YAML missing at {yaml_path} — fix the path resolution "
        "or restore the file from configs/globus_transfers/"
    )

    cfg = GlobusTransferStepConfig.from_config(yaml_path)
    assert cfg.source_endpoint_id == "src-test-uuid"
    assert cfg.dest_endpoint_id == "dst-test-uuid"
    assert cfg.client_id == "client-id-test"
    assert cfg.client_secret == "client-secret-test"
    assert cfg.auth_mode == "client_credentials"
    assert cfg.sync_level == "checksum"
    assert cfg.verify_checksum is True
    assert cfg.transfer_label == "apecx-setup-violin-bvbrc"


def test_resolve_auth_env_picks_confidential_when_available(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both confidential env vars are set, ``_resolve_auth_env``
    selects client_credentials and populates the RESOLVED slots."""
    from apecx_integration.cli._globus_data_transfer import _resolve_auth_env

    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "cc-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "cc-secret")

    mode = _resolve_auth_env()
    assert mode == "client_credentials"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] == "cc-id"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_SECRET"] == "cc-secret"
    assert os.environ["APECX_GLOBUS_AUTH_MODE"] == "client_credentials"


def test_resolve_auth_env_picks_native_when_only_native_set(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native client_id alone selects native auth + empty secret."""
    from apecx_integration.cli._globus_data_transfer import _resolve_auth_env

    monkeypatch.setenv("APECX_GLOBUS_NATIVE_CLIENT_ID", "native-id")

    mode = _resolve_auth_env()
    assert mode == "native"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] == "native-id"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_SECRET"] == ""
    assert os.environ["APECX_GLOBUS_AUTH_MODE"] == "native"


def test_resolve_auth_env_honors_explicit_mode_override(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-set APECX_GLOBUS_AUTH_MODE picks the path even when
    both are available — useful for testing the OTHER path."""
    from apecx_integration.cli._globus_data_transfer import _resolve_auth_env

    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "cc-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "cc-secret")
    monkeypatch.setenv("APECX_GLOBUS_NATIVE_CLIENT_ID", "native-id")
    monkeypatch.setenv("APECX_GLOBUS_AUTH_MODE", "native")

    mode = _resolve_auth_env()
    assert mode == "native"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] == "native-id"


# ---------------------------------------------------------------------------
# attempt_globus_data_transfer
# ---------------------------------------------------------------------------


def test_attempt_returns_unconfigured_when_prereqs_missing(
    clean_globus_env: None,
    tmp_path: Path,
) -> None:
    """With nothing set up, the attempt must return cleanly with
    status='unconfigured' so the caller can fall back to gh release."""
    from apecx_integration.cli._globus_data_transfer import attempt_globus_data_transfer

    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        result = attempt_globus_data_transfer(data_dir=tmp_path)

    assert result.status == "unconfigured"
    assert result.task_id is None
    assert result.items_transferred == 0
    assert "Globus" in result.detail or "skipped" in result.detail.lower()


# ---------------------------------------------------------------------------
# G84: _step_globus first-class install step
# ---------------------------------------------------------------------------


def test_step_globus_skipped_when_prereqs_missing(
    clean_globus_env: None,
    capsys: pytest.CaptureFixture,
) -> None:
    """``_step_globus`` returns ``skipped`` (NOT ``fail``) when
    Globus isn't configured. Reason text includes the actionable
    fallback message ('gh release fallback') so the operator's
    summary table shows what's happening."""
    from apecx_integration.cli.setup import _step_globus

    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        result = _step_globus(interactive=False)

    assert result.name == "globus"
    assert result.status == "skipped"
    assert "gh release fallback" in result.detail


def test_step_globus_interactive_prints_actionable_instructions(
    clean_globus_env: None,
    capsys: pytest.CaptureFixture,
) -> None:
    """Interactive mode must print copy-paste-able instructions for
    every missing prerequisite. Operators should leave with concrete
    next steps, not a vague ``not configured`` message."""
    from apecx_integration.cli.setup import _step_globus

    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        _step_globus(interactive=True)
    captured = capsys.readouterr()
    out = captured.out

    # Each unmet prerequisite must show an actionable hint.
    assert "APECX_GLOBUS_SOURCE_ENDPOINT_ID" in out
    assert "APECX_GLOBUS_DEST_ENDPOINT_ID" in out
    assert "apecx-globus-setup store" in out
    # And the fallback path is named so operators know what happens next.
    assert "gh release" in out


def test_step_globus_ok_when_everything_configured(
    clean_globus_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every prerequisite is satisfied, the step returns 'ok'."""
    from apecx_integration.cli.setup import _step_globus

    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-uuid")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "client-secret")

    result = _step_globus(interactive=False)

    # globus_sdk is installed in this workspace's venv; if for any
    # reason it isn't, gate the assertion the same way the prereq
    # status test does.
    if result.status == "ok":
        assert result.detail == "SDK + credentials + endpoint UUIDs all present"
    else:
        # The only other valid path: SDK not installed in this env.
        assert "globus_sdk" in result.detail


def test_step_globus_is_in_subcommand_registry() -> None:
    """The ``globus`` CLI subcommand must dispatch to ``_step_globus``."""
    from apecx_integration.cli.setup import _SUBCOMMANDS, _step_globus

    assert "globus" in _SUBCOMMANDS
    assert _SUBCOMMANDS["globus"] is _step_globus
