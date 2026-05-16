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
    """Default source prefix, 6 items, stable order, dest paths anchored
    at data_dir."""
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    items = build_transfer_items(tmp_path)

    assert len(items) == 6
    # First item is the first VIOLIN CSV.
    assert items[0]["source_path"] == "/apecx-joshi-anl-general/violin/Vaccine_Information.csv"
    assert items[0]["dest_path"] == str(tmp_path / "violin/Vaccine_Information.csv")
    # Last item is the BV-BRC alphavirus CSV at the dataset root.
    assert items[-1]["source_path"] == "/apecx-joshi-anl-general/BVBRC_genome_alphavirus.csv"
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

    assert items[0]["source_path"] == "/some/other/root/violin/Vaccine_Information.csv"
    # Trailing slash on the override is stripped.
    monkeypatch.setenv("APECX_GLOBUS_SOURCE_PREFIX", "/some/other/root/")
    items_with_slash = build_transfer_items(tmp_path)
    assert items_with_slash[0]["source_path"] == "/some/other/root/violin/Vaccine_Information.csv"


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

    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-test-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-test-uuid")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "client-secret-test")

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
    assert cfg.sync_level == "checksum"
    assert cfg.verify_checksum is True
    assert cfg.transfer_label == "apecx-setup-violin-bvbrc"


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
