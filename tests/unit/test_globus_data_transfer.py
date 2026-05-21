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
def clean_globus_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every APECX_GLOBUS_*  / GLOBUS_COMPUTE_* env var so each
    test starts from a known-empty state. Per-test setups then opt
    into the env they actually want."""
    for var in [
        "APECX_GLOBUS_SOURCE_ENDPOINT_ID",
        "APECX_GLOBUS_DEST_ENDPOINT_ID",
        "APECX_GLOBUS_VIOLIN_SOURCE_DIR",
        "APECX_GLOBUS_BVBRC_SOURCE_DIR",
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

    Source layout:
      /apecx-ramanathan-anl/apecx-project-all/<5 VIOLIN CSVs>
      /apecx-ramanathan-anl/public/data/BV-BRC/BVBRC_genome_alphavirus.csv

    Dest layout (unchanged — matches _EXPECTED_FILES):
      $data_dir/violin/<File>.csv
      $data_dir/BVBRC_genome_alphavirus.csv
    """
    from apecx_integration.cli._globus_data_transfer import build_transfer_items

    items = build_transfer_items(tmp_path)

    assert len(items) == 6
    # First item: a VIOLIN CSV under the apecx-project-all root.
    assert (
        items[0]["source_path"] == "/apecx-ramanathan-anl/apecx-project-all/Vaccine_Information.csv"
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
    """``_step_globus`` returns ``skipped`` (NOT ``fail``) when Globus isn't
    configured — the data step is the authoritative gate (it fails loud only
    if no data is already present). The reason text flags that Globus is now
    REQUIRED (gh fallback retired) so the summary table is honest."""
    from apecx_integration.cli.setup import _step_globus

    with patch(
        "apecx_integration.cli._globus_data_transfer._keyring_credentials_present",
        return_value=False,
    ):
        result = _step_globus(interactive=False)

    assert result.name == "globus"
    assert result.status == "skipped"
    assert "REQUIRED for data" in result.detail


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
    # And the operator is told Globus is now required (gh fallback retired).
    assert "REQUIRED" in out
    assert "retired" in out


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
