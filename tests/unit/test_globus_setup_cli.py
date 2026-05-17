"""Unit tests for the ``apecx-globus-setup`` CLI (G31).

Unconditional — no live Globus, no real OS keychain. The credential-
store-touching tests install a real *test* keyring backend
(``keyring.set_keyring`` with an in-memory ``KeyringBackend`` subclass)
and restore the original in teardown. That backend is a real keyring
backend, not a mock of the framework.

The ``test`` subcommand's live-Globus portion (endpoint status query,
``--round-trip`` dispatch) is NOT runnable here — no real credentials,
no real endpoint. It is covered up to the network boundary: the
credential-loading + app-building logic is exercised, and the network
steps are asserted to FAIL LOUD (you asked to test; it could not be
tested) rather than mocked into a misleading pass.

Covers:
  * argparse wiring — every subcommand parses; missing subcommand errors.
  * store -> status -> clear flow against a test keyring backend.
  * status never prints the secret value.
  * store FAIL-LOUDs (exit non-zero) on an insecure keyring backend.
  * endpoint-config renders the template with the project substituted.
  * test exits non-zero + prints a clear FAIL when no credentials exist.
  * test exits non-zero when credentials exist but no endpoint is given.
"""

from __future__ import annotations

import pytest

# The whole file tests apecx-globus-setup's keyring-touching subcommands
# via real keyring API (an in-memory test backend, not a mock). Without
# the ``keyring`` package installed there is no useful test surface —
# skip the whole file via the standard pytest gate. ``keyring`` is in
# apecx-integration's ``hpc`` extra; users not on the Globus path don't
# install it. Same pattern as nanobrain's test_globus_credentials.py.
pytest.importorskip(
    "keyring",
    reason=(
        "keyring not installed — apecx-globus-setup requires it. "
        "Install with `pip install keyring` or `pip install -e "
        "apecx-integration[hpc]` which bundles it."
    ),
)

from apecx_integration.cli.globus_setup import _build_parser, main


# ---------------------------------------------------------------------------
# test keyring backends — real KeyringBackend subclasses, not framework mocks
# ---------------------------------------------------------------------------
def _make_memory_backend():
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError

    class _MemoryTestKeyring(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self):
            super().__init__()
            self._store: dict = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            if (service, username) not in self._store:
                raise PasswordDeleteError("not found")
            del self._store[(service, username)]

    return _MemoryTestKeyring()


@pytest.fixture
def memory_keyring():
    """Install an in-memory test backend; restore the original in teardown."""
    import keyring

    original = keyring.get_keyring()
    backend = _make_memory_backend()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(original)


@pytest.fixture
def fail_keyring():
    """Install the real ``fail.Keyring`` backend (no usable secure store)."""
    import keyring
    import keyring.backends.fail

    original = keyring.get_keyring()
    keyring.set_keyring(keyring.backends.fail.Keyring())
    try:
        yield
    finally:
        keyring.set_keyring(original)


@pytest.fixture(autouse=True)
def _clear_globus_env(monkeypatch):
    """Every test starts with no Globus env vars set, for determinism."""
    monkeypatch.delenv("GLOBUS_COMPUTE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GLOBUS_COMPUTE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AURORA_GC_ENDPOINT_ID", raising=False)


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------
def test_every_subcommand_parses():
    parser = _build_parser()
    assert parser.parse_args(["store"]).subcommand == "store"
    assert parser.parse_args(["status"]).subcommand == "status"
    assert parser.parse_args(["clear"]).subcommand == "clear"

    test_args = parser.parse_args(["test"])
    assert test_args.subcommand == "test"
    assert test_args.endpoint_id is None
    assert test_args.round_trip is False

    test_args2 = parser.parse_args(["test", "--endpoint-id", "abc-123", "--round-trip"])
    assert test_args2.endpoint_id == "abc-123"
    assert test_args2.round_trip is True

    epc_args = parser.parse_args(
        ["endpoint-config", "--project", "MYALLOC", "--output", "/tmp/x.yml"]
    )
    assert epc_args.subcommand == "endpoint-config"
    assert epc_args.project == "MYALLOC"
    assert epc_args.output == "/tmp/x.yml"

    # G84+: test-transfer subcommand (added 2026-05-16).
    xfer_args = parser.parse_args(["test-transfer"])
    assert xfer_args.subcommand == "test-transfer"
    assert xfer_args.list_only is False
    assert xfer_args.source_path is None

    xfer_args2 = parser.parse_args(["test-transfer", "--list-only", "--source-path", "/foo/bar"])
    assert xfer_args2.list_only is True
    assert xfer_args2.source_path == "/foo/bar"


def test_login_subcommand_in_parser():
    """``apecx-globus-setup login`` parses + carries --client-id."""
    parser = _build_parser()
    args = parser.parse_args(["login"])
    assert args.subcommand == "login"
    assert args.client_id is None

    args2 = parser.parse_args(["login", "--client-id", "abc-uuid"])
    assert args2.client_id == "abc-uuid"


def test_login_fails_loud_without_client_id(capsys):
    """`apecx-globus-setup login` (no --client-id) exits non-zero with
    registration instructions. Operators get a copy-paste-able recipe;
    nothing is silently fabricated."""
    rc = main(["login"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "no --client-id supplied" in out
    assert "https://app.globus.org/settings/developers" in out
    assert "apecx-globus-setup login --client-id" in out


def test_native_auth_recognized_by_check_globus_prerequisites(monkeypatch):
    """G90: APECX_GLOBUS_NATIVE_CLIENT_ID being set counts as a valid
    credentials path, not a missing prereq. This is the bridge that
    lets `apecx-setup data` engage the Globus-first path when the
    operator uses native auth instead of confidential client."""
    from unittest.mock import patch

    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    for var in (
        "GLOBUS_COMPUTE_CLIENT_ID",
        "GLOBUS_COMPUTE_CLIENT_SECRET",
        "APECX_GLOBUS_SOURCE_ENDPOINT_ID",
        "APECX_GLOBUS_DEST_ENDPOINT_ID",
        "APECX_GLOBUS_NATIVE_CLIENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    # Native client_id set; no confidential creds; no keyring.
    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-uuid")
    monkeypatch.setenv("APECX_GLOBUS_NATIVE_CLIENT_ID", "native-uuid")
    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        status = check_globus_prerequisites()

    # If globus_sdk is installed in this env, every flag should be True.
    if status.sdk_installed:
        assert status.credentials_reachable is True
        assert status.configured is True


def test_test_transfer_fails_loud_on_missing_preconditions(monkeypatch, capsys):
    """``apecx-globus-setup test-transfer`` exits non-zero with a clear
    "fix the missing prerequisite" message when preconditions aren't met.
    This is the operator-facing version of the same check
    ``apecx-setup data`` does internally — proves the diagnostic path
    works when an operator hasn't finished Globus setup yet."""
    # Strip every Globus-related env var so preconditions cannot be met.
    for var in (
        "APECX_GLOBUS_SOURCE_ENDPOINT_ID",
        "APECX_GLOBUS_DEST_ENDPOINT_ID",
        "GLOBUS_COMPUTE_CLIENT_ID",
        "GLOBUS_COMPUTE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    # Patch the keyring probe so we get a deterministic "no creds" answer
    # regardless of what's in this developer's actual keyring.
    from unittest.mock import patch

    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        rc = main(["test-transfer", "--list-only"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "preconditions" in out
    # The operator gets actionable next-steps text.
    assert "fix the missing prerequisite" in out or "docs/globus_data_transfer.md" in out


def test_missing_subcommand_is_an_error():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_unknown_subcommand_is_an_error():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["frobnicate"])


# ---------------------------------------------------------------------------
# store -> status -> clear flow against a test keyring backend
# ---------------------------------------------------------------------------
def test_store_status_clear_flow(memory_keyring, monkeypatch, capsys):
    # store: feed client_id via input(), client_secret via getpass.
    monkeypatch.setattr("builtins.input", lambda _prompt="": "client-uuid-123")
    monkeypatch.setattr(
        "apecx_integration.cli.globus_setup.getpass.getpass",
        lambda _prompt="": "super-secret",
    )
    rc = main(["store"])
    assert rc == 0
    assert "PASS  store" in capsys.readouterr().out

    # status: shows the client_id, reports the secret as "set".
    rc = main(["status"])
    assert rc == 0
    status_out = capsys.readouterr().out
    assert "client-uuid-123" in status_out
    assert "client_secret    : set" in status_out

    # clear: removes both, idempotently.
    rc = main(["clear"])
    assert rc == 0
    assert "PASS  clear" in capsys.readouterr().out

    # status again: now nothing is stored.
    rc = main(["status"])
    assert rc == 0
    status_out2 = capsys.readouterr().out
    assert "client_id        : (not set)" in status_out2
    assert "client_secret    : not set" in status_out2


def test_clear_is_idempotent(memory_keyring, capsys):
    # Clearing an empty store is a success.
    assert main(["clear"]) == 0
    assert main(["clear"]) == 0


def test_store_rejects_empty_client_id(memory_keyring, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "   ")
    monkeypatch.setattr(
        "apecx_integration.cli.globus_setup.getpass.getpass",
        lambda _prompt="": "secret",
    )
    rc = main(["store"])
    assert rc == 1
    assert "FAIL  store" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# status never prints the secret value
# ---------------------------------------------------------------------------
def test_status_never_prints_the_secret(memory_keyring, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "client-uuid-123")
    monkeypatch.setattr(
        "apecx_integration.cli.globus_setup.getpass.getpass",
        lambda _prompt="": "TOP-SECRET-DO-NOT-LEAK",
    )
    main(["store"])
    capsys.readouterr()  # drain store output

    main(["status"])
    status_out = capsys.readouterr().out
    assert "TOP-SECRET-DO-NOT-LEAK" not in status_out
    # ...but it confirms a secret IS set.
    assert "client_secret    : set" in status_out


# ---------------------------------------------------------------------------
# store FAIL-LOUDs on an insecure keyring backend — no plaintext fallback
# ---------------------------------------------------------------------------
def test_store_fails_loud_on_insecure_backend(fail_keyring, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "client-uuid-123")
    monkeypatch.setattr(
        "apecx_integration.cli.globus_setup.getpass.getpass",
        lambda _prompt="": "secret",
    )
    rc = main(["store"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL  store" in out
    # The framework's actionable insecure-backend message is surfaced.
    assert "not a secure credential store" in out


def test_status_reports_insecure_backend(fail_keyring, capsys):
    rc = main(["status"])
    assert rc == 0  # status itself works; it just reports the bad backend
    out = capsys.readouterr().out
    assert "backend secure   : NO" in out


# ---------------------------------------------------------------------------
# endpoint-config renders the template with the project substituted
# ---------------------------------------------------------------------------
def test_endpoint_config_renders_with_project(tmp_path, capsys):
    output = tmp_path / "rendered-config.yaml"
    rc = main(["endpoint-config", "--project", "MYALLOC", "--output", str(output)])
    assert rc == 0
    assert output.exists()
    rendered = output.read_text(encoding="utf-8")
    # The placeholder is gone; the project name is in.
    assert "<YOUR_ALCF_PROJECT>" not in rendered
    assert "MYALLOC" in rendered
    # The # VERIFY reminder is printed to the operator.
    out = capsys.readouterr().out
    assert "PASS  endpoint-config" in out
    assert "VERIFY" in out


def test_endpoint_config_without_project_leaves_placeholder(tmp_path, capsys):
    output = tmp_path / "rendered-config.yaml"
    rc = main(["endpoint-config", "--output", str(output)])
    assert rc == 0
    rendered = output.read_text(encoding="utf-8")
    # No --project: the placeholder is left for manual fill-in.
    assert "<YOUR_ALCF_PROJECT>" in rendered
    assert "left as-is" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# test subcommand — up to the network boundary (live Globus not runnable here)
# ---------------------------------------------------------------------------
def test_test_fails_loud_when_no_credentials(memory_keyring, capsys):
    """No creds in env OR the (empty) keyring store -> clear FAIL, exit 1."""
    rc = main(["test"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL  credentials" in out
    # It must NOT print a misleading pass.
    assert "PASS  test" not in out


def test_test_fails_loud_when_credentials_present_but_no_endpoint(
    memory_keyring, monkeypatch, capsys
):
    """Credentials resolve (from env) but no endpoint id -> FAIL, exit 1.

    This exercises the credential-loading + GlobusApp-build path (the
    unit-testable portion) and confirms the missing-endpoint network
    precondition FAILs loudly rather than being skipped silently.
    """
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "env-client-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "env-client-secret")
    # No --endpoint-id, no $AURORA_GC_ENDPOINT_ID.
    rc = main(["test"])
    assert rc == 1
    out = capsys.readouterr().out
    # Credentials + auth-app build are the unit-testable portion: they pass.
    assert "PASS  credentials" in out
    assert "PASS  globus auth" in out
    # The endpoint id is missing -> loud FAIL, non-zero exit.
    assert "FAIL  endpoint id" in out
    assert "PASS  test" not in out


def test_test_resolves_credentials_from_keyring(memory_keyring, monkeypatch, capsys):
    """With no env vars, ``test`` falls back to the keyring credential store."""
    from nanobrain.core.distributed import globus_credentials

    globus_credentials.store_credentials("keyring-id", "keyring-secret")
    # No endpoint -> still FAILs, but the credentials step must PASS and
    # report the keyring as the source.
    rc = main(["test"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "PASS  credentials" in out
    assert "OS secure store" in out
