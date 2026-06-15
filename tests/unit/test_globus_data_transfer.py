"""Unit tests for the Globus data-transfer glue (G82 2026-05-16; G127 2026-05-21).

What's covered
--------------

* ``check_globus_prerequisites`` correctly identifies every failure
  mode (no SDK, no source endpoint, no dest endpoint, no credentials)
  and the success case.
* ``_keyring_credentials_present`` delegates to nanobrain's
  ``load_credentials`` (the 2026-05-21 service-name fix).
* ``build_transfer_items`` produces the re-mapped layout (5 VIOLIN CSVs +
  curated BV-BRC) with independently-overridable source roots.
* The verify→transfer workflow YAML loads as a ``Workflow`` with both steps
  and DirectLinks that all carry ``auto_transfer`` (silent-no-op guard).
* The same topology is expressible via the lightweight ``WorkflowBuilder``.
* The transfer step wrapper YAML parses via ``GlobusTransferStepConfig``.

What's NOT covered here
-----------------------

A live transfer against a real Globus endpoint is in the gated integration
suite (``tests/integration/test_globus_transfer_live.py``): the missing-source
gate runs against real source auth; the full transfer needs a real writable
dest endpoint.
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
def clean_globus_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Strip every APECX_GLOBUS_*  / GLOBUS_COMPUTE_* env var so each
    test starts from a known-empty state. Per-test setups then opt
    into the env they actually want. Also points the persisted Globus
    config at a non-existent temp path so build_transfer_items /
    dest-endpoint resolution never read the dev machine's real
    ~/.apecx/globus_config.json (which could carry extra dirs)."""
    for var in [
        "APECX_GLOBUS_SOURCE_ENDPOINT_ID",
        "APECX_GLOBUS_DEST_ENDPOINT_ID",
        "APECX_GLOBUS_VIOLIN_SOURCE_DIR",
        "APECX_GLOBUS_BVBRC_SOURCE_DIR",
        "APECX_GLOBUS_AUTH_MODE",
        "APECX_GLOBUS_NATIVE_CLIENT_ID",
        "APECX_GLOBUS_RESOLVED_CLIENT_ID",
        "APECX_GLOBUS_RESOLVED_CLIENT_SECRET",
        "GLOBUS_COMPUTE_CLIENT_ID",
        "GLOBUS_COMPUTE_CLIENT_SECRET",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APECX_GLOBUS_CONFIG_PATH", str(tmp_path / "no_globus_config.json"))


# ---------------------------------------------------------------------------
# check_globus_prerequisites
# ---------------------------------------------------------------------------


def test_prereqs_native_default_only_endpoints_gate(clean_globus_env: None) -> None:
    """NATIVE is the default auth mode (2026-05-21). Native needs no pre-stored
    secret — the built-in native client_id always resolves and the token comes
    from the browser login — so credentials_reachable is True even with nothing
    set; the only gates are SDK + the two endpoint UUIDs."""
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    status = check_globus_prerequisites()  # nothing set → native default

    assert status.credentials_reachable is True  # native needs no stored creds
    assert status.source_endpoint_set is False
    assert status.dest_endpoint_set is False
    assert status.configured is False  # endpoints missing
    reason = status.reason()
    assert "APECX_GLOBUS_SOURCE_ENDPOINT_ID unset" in reason
    assert "APECX_GLOBUS_DEST_ENDPOINT_ID unset" in reason


def test_prereqs_native_configured_with_endpoints_only(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native default: SDK + both endpoints set → configured, no creds needed."""
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-uuid")

    status = check_globus_prerequisites()
    if status.sdk_installed:
        assert status.credentials_reachable is True
        assert status.configured is True
        assert status.reason() == "Globus prerequisites OK"


def test_prereqs_client_credentials_optin_requires_creds(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in secret path (APECX_GLOBUS_AUTH_MODE=client_credentials) DOES
    require a real id+secret — from env or keyring. Missing both → not reachable
    + the 'no client credentials' reason."""
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    monkeypatch.setenv("APECX_GLOBUS_AUTH_MODE", "client_credentials")
    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-uuid")

    # No env creds + empty keyring → not reachable.
    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        status = check_globus_prerequisites()
    assert status.credentials_reachable is False
    assert status.configured is False
    assert "no client credentials" in status.reason()

    # Env creds → reachable.
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "cid")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "secret")
    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        assert check_globus_prerequisites().credentials_reachable is True

    # Keyring creds (no env) → also reachable.
    monkeypatch.delenv("GLOBUS_COMPUTE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GLOBUS_COMPUTE_CLIENT_SECRET", raising=False)
    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=True,
    ):
        assert check_globus_prerequisites().credentials_reachable is True


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


def test_keyring_present_delegates_to_nanobrain_load_credentials() -> None:
    """Regression (2026-05-21): the preflight MUST read credentials from the
    same place ``build_globus_app`` does — nanobrain's
    ``globus_credentials.load_credentials`` (keyring service ``nanobrain-globus``)
    — NOT a hardcoded ``apecx-globus-setup`` service name. The old hardcoded
    name made the preflight report 'not configured' while valid creds existed
    and build_globus_app would have found them; with gh retired that mismatch
    turns a working setup into a hard install failure."""
    from apecx_integration.cli._globus_data_transfer import _keyring_credentials_present

    # Creds present under the nanobrain service → True.
    with patch(
        "nanobrain.core.distributed.globus_credentials.load_credentials",
        return_value=("client-uuid", "secret"),
    ):
        assert _keyring_credentials_present() is True

    # Nothing stored → False.
    with patch(
        "nanobrain.core.distributed.globus_credentials.load_credentials",
        return_value=(None, None),
    ):
        assert _keyring_credentials_present() is False

    # Loader raising (e.g. keyring not installed) → False, never propagates.
    with patch(
        "nanobrain.core.distributed.globus_credentials.load_credentials",
        side_effect=RuntimeError("keyring missing"),
    ):
        assert _keyring_credentials_present() is False


# ---------------------------------------------------------------------------
# build_transfer_items
# ---------------------------------------------------------------------------


def test_build_transfer_items_default_layout(clean_globus_env: None, tmp_path: Path) -> None:
    """Default layout (re-mapped 2026-05-21): 5 VIOLIN CSVs first, then the
    curated BV-BRC file, with VIOLIN and BV-BRC under DIFFERENT source roots.

    Source layout (both live-verified on collection 8d2e71d6, 2026-05-21):
      /apecx-ramanathan-anl/apecx-project-all/violin/<5 VIOLIN CSVs>
      /apecx-ramanathan-anl/public/data/BV-BRC/BVBRC_genome_alphavirus.csv

    Dest layout (unchanged — matches _EXPECTED_FILES):
      $data_dir/violin/<File>.csv
      $data_dir/BVBRC_genome_alphavirus.csv
    """
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    items = build_transfer_items(tmp_path)

    assert len(items) == 6
    # First item: a VIOLIN CSV under the apecx-project-all/violin/ root.
    assert (
        items[0]["source_path"]
        == "/apecx-ramanathan-anl/apecx-project-all/violin/Vaccine_Information.csv"
    )
    assert items[0]["dest_path"] == str(tmp_path / "violin/Vaccine_Information.csv")
    # Last item: the curated BV-BRC alphavirus file under /public (no rename —
    # source filename == dest filename; content divergence fixed).
    assert (
        items[-1]["source_path"]
        == "/apecx-ramanathan-anl/public/data/BV-BRC/BVBRC_genome_alphavirus.csv"
    )
    assert items[-1]["dest_path"] == str(tmp_path / "BVBRC_genome_alphavirus.csv")
    for item in items:
        assert set(item.keys()) == {"source_path", "dest_path"}


def test_build_transfer_items_honors_independent_root_overrides(
    clean_globus_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VIOLIN and BV-BRC source roots are INDEPENDENTLY overridable (they live
    under different parents now), and trailing slashes are stripped."""
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    monkeypatch.setenv("APECX_GLOBUS_VIOLIN_SOURCE_DIR", "/violin/root/")
    monkeypatch.setenv("APECX_GLOBUS_BVBRC_SOURCE_DIR", "/bvbrc/root/")
    items = build_transfer_items(tmp_path)

    assert items[0]["source_path"] == "/violin/root/Vaccine_Information.csv"
    assert items[-1]["source_path"] == "/bvbrc/root/BVBRC_genome_alphavirus.csv"
    # Dest layout is unchanged — downstream readers keep working.
    assert items[0]["dest_path"] == str(tmp_path / "violin/Vaccine_Information.csv")
    assert items[-1]["dest_path"] == str(tmp_path / "BVBRC_genome_alphavirus.csv")


def test_build_transfer_items_includes_extra_dirs_recursive(
    clean_globus_env: None, tmp_path: Path
) -> None:
    """A user-registered extra dir appears as a RECURSIVE item (datasets=None
    includes the 'extra' group)."""
    from apecx_integration.cli import globus_config
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    globus_config.add_source_dir("/apecx-ramanathan-anl/foo/mydata", dest_subdir="mydata")
    items = build_transfer_items(tmp_path)

    extra = [i for i in items if i.get("recursive")]
    assert len(extra) == 1
    assert extra[0]["source_path"] == "/apecx-ramanathan-anl/foo/mydata"
    assert extra[0]["dest_path"] == str(tmp_path / "mydata")
    assert extra[0]["recursive"] is True
    # The 6 built-in items are unchanged (no recursive key).
    assert sum(1 for i in items if "recursive" not in i) == 6


def test_build_transfer_items_extra_excluded_when_dataset_scoped(
    clean_globus_env: None, tmp_path: Path
) -> None:
    """A required-only call (datasets={'bvbrc'}) must NOT pull extra dirs."""
    from apecx_integration.cli import globus_config
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    globus_config.add_source_dir("/x/y")
    items = build_transfer_items(tmp_path, datasets={"bvbrc"})
    assert all(not i.get("recursive") for i in items)
    assert len(items) == 1  # just the BV-BRC file


def test_dest_endpoint_falls_back_to_config(clean_globus_env: None) -> None:
    """check_globus_prerequisites reports dest set when only the persisted
    config (not the env var) carries it."""
    from apecx_integration.cli import globus_config
    from apecx_integration.cli._globus_data_transfer import check_globus_prerequisites

    globus_config.set_dest_endpoint("dest-from-config")
    status = check_globus_prerequisites()
    assert status.dest_endpoint_set is True


def test_backfill_dest_endpoint_env_populates_from_config(clean_globus_env: None) -> None:
    from apecx_integration.cli import globus_config
    from apecx_integration.cli._globus_data_transfer import _backfill_dest_endpoint_env

    globus_config.set_dest_endpoint("ep-123")
    assert "APECX_GLOBUS_DEST_ENDPOINT_ID" not in os.environ
    _backfill_dest_endpoint_env()
    assert os.environ["APECX_GLOBUS_DEST_ENDPOINT_ID"] == "ep-123"


def test_backfill_dest_endpoint_env_respects_existing(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apecx_integration.cli import globus_config
    from apecx_integration.cli._globus_data_transfer import _backfill_dest_endpoint_env

    globus_config.set_dest_endpoint("from-config")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "from-env")
    _backfill_dest_endpoint_env()
    assert os.environ["APECX_GLOBUS_DEST_ENDPOINT_ID"] == "from-env"  # env wins


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


def test_verify_transfer_workflow_loads_and_all_links_auto_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify→transfer workflow YAML must load as a Workflow with both
    child steps AND every DirectLink declaring auto_transfer:true (the dominant
    nanobrain silent-no-op shape). Guards against a future edit dropping the
    flag or mis-wiring the gate."""
    # The workflow references GlobusManifestVerifyStep (nanobrain G127) by class
    # path; Workflow.from_config imports it. A clean install pulls nanobrain from
    # git@academy-integration — skip cleanly until that ref ships G127 (rather
    # than erroring CI). Runs as soon as the dep is present.
    pytest.importorskip(
        "nanobrain.library.steps.globus_manifest_verify_step",
        reason="nanobrain GlobusManifestVerifyStep (G127) not in the installed nanobrain yet",
    )
    from nanobrain.core.workflow import Workflow

    from apecx_integration.cli._globus_data_transfer import (
        _resolve_auth_env,
        _workflow_yaml_path,
    )

    monkeypatch.setenv("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "src-test-uuid")
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "dst-test-uuid")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "cc-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "cc-secret")
    _resolve_auth_env()  # populate the RESOLVED slots the step YAMLs read

    yaml_path = _workflow_yaml_path()
    assert yaml_path.is_file(), f"workflow YAML missing at {yaml_path}"

    wf = Workflow.from_config(yaml_path)
    assert set(wf.child_steps.keys()) == {"verify", "transfer"}

    links = wf.step_links
    direct_links = [link for link in links.values() if type(link).__name__ == "DirectLink"]
    assert len(direct_links) == 5, f"expected 5 DirectLinks, got {len(direct_links)}"
    for link in direct_links:
        assert getattr(link.config, "auto_transfer", None) is True, (
            f"DirectLink {getattr(link, 'name', link)!r} must declare "
            "auto_transfer:true (silent-no-op guard)"
        )


def test_workflow_builder_lightweight_parity() -> None:
    """The verify→transfer topology is ALSO expressible via the lightweight
    ``WorkflowBuilder`` (programmatic path), not only hand-authored YAML —
    demonstrating the multiple framework-native authoring paths.

    Asserts the builder ENCODES the topology faithfully: both steps with the
    correct class paths, and three ``DirectLink`` entries in the nested shape
    LinkBase.from_config expects (auto_transfer is injected by the v2
    model_validator at load time, not stored in the builder config).

    Scope note: this asserts the builder's generated config rather than a full
    ``.load()`` round-trip. The builder emits FLAT step entries
    (``{name, class, ...inline_fields}``); steps carrying inline data units do
    not materialize cleanly through ``Workflow.from_config``'s step loader (an
    orthogonal builder/loader limitation, documented in the outcomes doc). The
    runtime equivalence of this topology is proven by the hand-authored YAML
    path's live test, which uses the same step classes + link shape."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    _DU = "nanobrain.core.data_unit.DataUnitMemory"
    _TRIG = "nanobrain.core.trigger.DataUnitChangeTrigger"
    verify_cls = "nanobrain.library.steps.globus_manifest_verify_step.GlobusManifestVerifyStep"
    transfer_cls = "nanobrain.library.steps.globus_transfer_step.GlobusTransferStep"

    builder = WorkflowBuilder("violin_bvbrc_transfer_builder", "verify→transfer (builder)")
    builder.add_input("workflow_input")
    builder.add_output("transfer_status")
    builder.add_step(
        "verify",
        verify_cls,
        source_endpoint_id="src-dummy-uuid",
        input_data_units={"manifest_in": {"class": _DU, "name": "manifest_in"}},
        output_data_units={"verified_manifest": {"class": _DU, "name": "verified_manifest"}},
        triggers=[{"class": _TRIG, "data_unit": "manifest_in"}],
    )
    builder.add_step(
        "transfer",
        transfer_cls,
        source_endpoint_id="src-dummy-uuid",
        dest_endpoint_id="dst-dummy-uuid",
        input_data_units={"transfer_input": {"class": _DU, "name": "transfer_input"}},
        output_data_units={"status": {"class": _DU, "name": "status"}},
        triggers=[{"class": _TRIG, "data_unit": "transfer_input"}],
    )
    builder.connect("workflow_input", "verify.manifest_in")
    builder.connect("verify.verified_manifest", "transfer.transfer_input")
    builder.connect("transfer.status", "transfer_status")

    cfg = builder.get_config()
    # Both steps encoded with the right class paths.
    assert cfg["steps"]["verify"]["class"] == verify_cls
    assert cfg["steps"]["transfer"]["class"] == transfer_cls
    # Three DirectLinks in the nested {class, config:{link_type, source, target}}
    # shape, wiring workflow_input → verify → transfer → workflow_output.
    links = list(cfg["links"].values())
    assert len(links) == 3
    edges = {(link["config"]["source"], link["config"]["target"]) for link in links}
    assert edges == {
        ("workflow_input", "verify.manifest_in"),
        ("verify.verified_manifest", "transfer.transfer_input"),
        ("transfer.status", "transfer_status"),
    }
    for link in links:
        assert link["class"].endswith("DirectLink")
        assert link["config"]["link_type"] == "direct"


def test_resolve_auth_env_defaults_to_native(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-05-21: native (web) is the DEFAULT. With NOTHING set, _resolve_auth_env
    selects native, resolves the built-in native client_id, and leaves the secret
    empty."""
    from apecx_integration.cli._globus_data_transfer import (
        _DEFAULT_NATIVE_CLIENT_ID,
        _resolve_auth_env,
    )

    mode = _resolve_auth_env()
    assert mode == "native"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] == _DEFAULT_NATIVE_CLIENT_ID
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_SECRET"] == ""
    assert os.environ["APECX_GLOBUS_AUTH_MODE"] == "native"


def test_resolve_auth_env_native_default_ignores_confidential_creds(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The secret path is OPT-IN: even when confidential env creds are present,
    the default stays native unless explicitly selected. (Secret is a separate
    option, not an auto-pick.)"""
    from apecx_integration.cli._globus_data_transfer import _resolve_auth_env

    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "cc-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "cc-secret")

    assert _resolve_auth_env() == "native"
    assert os.environ["APECX_GLOBUS_AUTH_MODE"] == "native"


def test_resolve_auth_env_native_honors_custom_client_id(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom native client_id overrides the built-in default."""
    from apecx_integration.cli._globus_data_transfer import _resolve_auth_env

    monkeypatch.setenv("APECX_GLOBUS_NATIVE_CLIENT_ID", "my-native-id")
    assert _resolve_auth_env() == "native"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] == "my-native-id"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_SECRET"] == ""


def test_resolve_auth_env_client_credentials_optin(
    clean_globus_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt into the secret path with APECX_GLOBUS_AUTH_MODE=client_credentials;
    the RESOLVED slots get the confidential env creds."""
    from apecx_integration.cli._globus_data_transfer import _resolve_auth_env

    monkeypatch.setenv("APECX_GLOBUS_AUTH_MODE", "client_credentials")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_ID", "cc-id")
    monkeypatch.setenv("GLOBUS_COMPUTE_CLIENT_SECRET", "cc-secret")

    mode = _resolve_auth_env()
    assert mode == "client_credentials"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] == "cc-id"
    assert os.environ["APECX_GLOBUS_RESOLVED_CLIENT_SECRET"] == "cc-secret"


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
    """``_step_globus`` returns ``skipped`` (NOT ``fail``) when Globus isn't
    configured, and the reason text flags that Globus is OPTIONAL (only the
    offline ``query_*`` data tools need it) so the summary table is honest and
    a fresh, fully-functional install isn't made to look broken."""
    from apecx_integration.cli.setup import _step_globus

    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        result = _step_globus(interactive=False)

    assert result.name == "globus"
    assert result.status == "skipped"
    assert "OPTIONAL" in result.detail


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
    # Default auth is web-based (login); the secret path is the opt-in option.
    assert "apecx-globus-setup login" in out
    assert "APECX_GLOBUS_AUTH_MODE=client_credentials" in out
    assert "apecx-globus-setup store" in out
    # And the operator is told Globus is OPTIONAL (only the offline query_* tools
    # need it) — a fresh install without it is fully functional, not broken.
    assert "OPTIONAL" in out
    assert "query_*" in out


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


# ---------------------------------------------------------------------------
# build_transfer_items dataset filtering (REQUIRED bvbrc / OPTIONAL violin)
# ---------------------------------------------------------------------------
def test_build_transfer_items_bvbrc_only(clean_globus_env: None, tmp_path: Path) -> None:
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    items = build_transfer_items(tmp_path, datasets={"bvbrc"})
    assert len(items) == 1
    assert items[0]["dest_path"] == str(tmp_path / "BVBRC_genome_alphavirus.csv")


def test_build_transfer_items_violin_only(clean_globus_env: None, tmp_path: Path) -> None:
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    items = build_transfer_items(tmp_path, datasets={"violin"})
    assert len(items) == 5
    assert all("/violin/" in it["dest_path"] for it in items)


def test_build_transfer_items_unknown_dataset_raises(
    clean_globus_env: None, tmp_path: Path
) -> None:
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    with pytest.raises(ValueError, match="unknown dataset"):
        build_transfer_items(tmp_path, datasets={"nope"})


# ---------------------------------------------------------------------------
# _step_data: REQUIRED (BV-BRC) must succeed; OPTIONAL (VIOLIN) warn-on-fail
# ---------------------------------------------------------------------------
def _configured_prereqs():
    from apecx_integration.cli._globus_data_transfer import GlobusPrereqStatus

    return GlobusPrereqStatus(
        configured=True,
        sdk_installed=True,
        source_endpoint_set=True,
        dest_endpoint_set=True,
        credentials_reachable=True,
        detail="ok",
    )


def _patch_step_data(monkeypatch, tmp_path, attempt_fn):
    """Wire _step_data's dependencies: configured prereqs, a data dir, a fake
    transfer, and no-op config patch / layout report."""
    import apecx_integration.cli._globus_data_transfer as gdt
    import apecx_integration.cli.setup_data as sd

    monkeypatch.setattr(gdt, "check_globus_prerequisites", _configured_prereqs)
    monkeypatch.setattr(gdt, "attempt_globus_data_transfer", attempt_fn)
    monkeypatch.setattr(sd, "prompt_for_data_dir", lambda *, interactive=True: tmp_path)
    monkeypatch.setattr(sd, "report_post_transfer_layout", lambda data_dir: [])
    monkeypatch.setattr(sd, "_maybe_update_claude_config", lambda data_dir: None)


def _result(status, detail="d", task_id=None):
    from apecx_integration.cli._globus_data_transfer import GlobusTransferResult

    return GlobusTransferResult(status=status, detail=detail, task_id=task_id)


def test_step_data_ok_when_both_datasets_transfer(monkeypatch, tmp_path, capsys):
    from apecx_integration.cli.setup import _step_data

    def attempt(*, data_dir, datasets=None, poll_timeout_seconds=600.0):
        return _result("ok", "transferred", task_id="t-" + next(iter(datasets)))

    _patch_step_data(monkeypatch, tmp_path, attempt)
    res = _step_data(interactive=True)
    assert res.status == "ok"
    assert "BV-BRC + VIOLIN" in res.detail


def test_step_data_partial_when_violin_fails_but_bvbrc_ok(monkeypatch, tmp_path, capsys):
    """The headline behavior: VIOLIN unavailable → loud warning + 'partial'
    (install completes), NOT a hard failure."""
    from apecx_integration.cli.setup import _step_data

    def attempt(*, data_dir, datasets=None, poll_timeout_seconds=600.0):
        if datasets == {"bvbrc"}:
            return _result("ok", "bvbrc done", task_id="t1")
        return _result("fail", "verify gate: not a member of 'apecx-project-all' Group")

    _patch_step_data(monkeypatch, tmp_path, attempt)
    res = _step_data(interactive=True)
    assert res.status == "partial"
    out = capsys.readouterr().out
    assert "VIOLIN data was NOT transferred" in out
    assert "OPTIONAL" in out
    assert "apecx-project-all" in out  # actionable Group hint surfaced


def test_step_data_fail_when_required_bvbrc_fails(monkeypatch, tmp_path, capsys):
    from apecx_integration.cli.setup import _step_data

    def attempt(*, data_dir, datasets=None, poll_timeout_seconds=600.0):
        if datasets == {"bvbrc"}:
            return _result("fail", "endpoint not active")
        return _result("ok")  # should never be reached

    _patch_step_data(monkeypatch, tmp_path, attempt)
    res = _step_data(interactive=True)
    assert res.status == "fail"
    assert "BV-BRC" in res.detail


def test_step_data_skips_not_fails_when_unconfigured_and_no_local_data(
    monkeypatch, tmp_path, capsys
):
    """Clean install (no Globus, no local CSVs) must SKIP, NOT fail.

    Regression for the confusing "❌ data — Globus required but not configured"
    hard failure on a fresh install. Local CSVs are OPTIONAL — harmonized_search
    uses the anonymous public Globus index and the dictionary auto-downloads, so a
    missing local dataset must never make a fully-functional install look broken.
    """
    import apecx_integration.cli._globus_data_transfer as gdt
    import apecx_integration.cli.setup_data as sd
    from apecx_integration.cli._globus_data_transfer import GlobusPrereqStatus
    from apecx_integration.cli.setup import _step_data

    unconfigured = GlobusPrereqStatus(
        configured=False,
        sdk_installed=True,
        source_endpoint_set=False,
        dest_endpoint_set=False,
        credentials_reachable=True,
        detail="endpoints unset",
    )
    monkeypatch.setattr(gdt, "check_globus_prerequisites", lambda: unconfigured)
    # Point the default data dir at an EMPTY tmp dir → no local BV-BRC CSV present.
    monkeypatch.setattr(sd, "_DEFAULT_DATA_DIR", tmp_path)

    res = _step_data(interactive=True)

    assert res.status == "skipped", res
    assert "OPTIONAL" in res.detail
    out = capsys.readouterr().out
    assert "SKIPPING" in out
    assert "harmonized_search" in out
    assert "❌" not in out  # no alarming failure marker on a healthy fresh install
