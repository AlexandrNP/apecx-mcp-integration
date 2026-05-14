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
