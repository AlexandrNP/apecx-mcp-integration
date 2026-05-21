"""Gated live integration tests for the Globus verify→transfer data path (G127).

Real-dependency parity for the mocked unit tests in
``tests/unit/test_globus_data_transfer.py`` (workspace mock-parity rule). These
drive the REAL verify→transfer nanobrain workflow against a REAL Globus
collection — no mocks.

Two independently-gated tests:

  * ``test_missing_source_gate_fails_loud`` — needs only SOURCE-side creds. It
    points the manifest at a bogus source file and asserts the driver returns
    ``status='fail'`` naming the missing path: the verify gate blocks the
    transfer (which never runs, so a dummy dest endpoint is fine). This is the
    real-data proof that ``Workflow.run`` swallowing the verify exception does
    NOT become a false success.

  * ``test_full_transfer_succeeds`` — needs a REAL writable dest endpoint
    (Globus Connect Personal) + ``APECX_GLOBUS_LIVE_TRANSFER=1``. Skips
    otherwise. Asserts the dataset lands in the ``_EXPECTED_FILES`` layout.

Set ``APECX_GLOBUS_SOURCE_ENDPOINT_ID`` (the APECx data collection UUID) +
resolvable confidential-client creds (``apecx-globus-setup store`` or the env
vars) to enable the first test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_SOURCE_EP = os.environ.get("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "").strip()


def _source_creds_ok() -> bool:
    """True iff a source endpoint is set AND confidential creds resolve."""
    if not _SOURCE_EP:
        return False
    try:
        from nanobrain.core.distributed.globus_auth import build_globus_app

        build_globus_app(
            auth_mode="client_credentials",
            scopes=["urn:globus:auth:scope:transfer.api.globus.org:all"],
            app_name="apecx-globus-live-test",
        )
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _source_creds_ok(),
    reason=(
        "live Globus test needs APECX_GLOBUS_SOURCE_ENDPOINT_ID + resolvable "
        "confidential-client credentials"
    ),
)


def test_missing_source_gate_fails_loud(tmp_path, monkeypatch):
    """The real verify→transfer workflow, driven through the production entry
    point, must FAIL LOUD when a source file is missing — never report a false
    success (the Workflow.run-swallows-exceptions trap)."""
    import apecx_integration.cli._globus_data_transfer as g

    # Dummy dest endpoint: the transfer step never runs (verify blocks first),
    # so this is never contacted — but it satisfies the prereq check.
    monkeypatch.setenv("APECX_GLOBUS_DEST_ENDPOINT_ID", "00000000-0000-0000-0000-000000000000")
    # Bogus source path under the (real, reachable) public collection.
    bogus = "/apecx-ramanathan-anl/public/data/BV-BRC/__nanobrain_missing__.csv"
    monkeypatch.setattr(
        g,
        "build_transfer_items",
        lambda data_dir, datasets=None: [
            {"source_path": bogus, "dest_path": str(Path(data_dir) / "x.csv")}
        ],
    )

    result = g.attempt_globus_data_transfer(data_dir=tmp_path)

    assert result.status == "fail", f"expected loud fail, got {result.status}: {result.detail}"
    assert "__nanobrain_missing__.csv" in (result.detail + (result.error or ""))


@pytest.mark.skipif(
    os.environ.get("APECX_GLOBUS_LIVE_TRANSFER", "") != "1"
    or not os.environ.get("APECX_GLOBUS_DEST_ENDPOINT_ID", "").strip()
    or os.environ.get("APECX_GLOBUS_DEST_ENDPOINT_ID", "").strip().startswith("00000000"),
    reason=(
        "full live transfer needs APECX_GLOBUS_LIVE_TRANSFER=1 + a REAL writable "
        "APECX_GLOBUS_DEST_ENDPOINT_ID (Globus Connect Personal running)"
    ),
)
def test_full_transfer_succeeds(tmp_path):
    """End-to-end: verify passes, transfer moves the dataset, files land in the
    _EXPECTED_FILES layout. Only runs with a real dest endpoint."""
    from apecx_integration.cli._globus_data_transfer import attempt_globus_data_transfer
    from apecx_integration.cli.setup_data import _EXPECTED_FILES

    result = attempt_globus_data_transfer(data_dir=tmp_path)
    assert result.status == "ok", f"transfer failed: {result.detail}"
    missing = [f for f in _EXPECTED_FILES if not (tmp_path / f).exists()]
    assert not missing, f"transfer ok but files missing on disk: {missing}"
