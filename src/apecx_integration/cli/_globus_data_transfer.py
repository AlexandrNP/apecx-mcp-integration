"""Apecx-side glue for the Globus-first data transfer (G82, 2026-05-16).

The actual Globus Transfer primitive is nanobrain's
``GlobusTransferStep`` (``library/steps/globus_transfer_step.py``).
This module:

  1. Checks operator preconditions (globus_sdk installed, endpoint UUIDs
     set, credentials reachable) and either reports them as a
     ``GlobusPrereqStatus`` or attempts the transfer.
  2. Builds the runtime ``items`` payload — the 6 VIOLIN/BV-BRC CSVs,
     each mapped from the source ``apecx-joshi-anl-general`` path to
     the operator's chosen local data directory.
  3. Materializes the nanobrain ``GlobusTransferStep`` via the wrapper
     YAML at ``configs/globus_transfers/violin_bvbrc_transfer_step.yml``
     and ``await``s its ``process()`` to drive the transfer.

The wrapper YAML is the only nanobrain-framework-native config; the
Python here is a thin orchestrator that picks the right inputs and
surfaces a structured result to ``cli/setup.py:_step_data``.

Brutal-truth design notes
-------------------------

* This module **prefers** Globus but does NOT require it. When the
  preconditions are unmet, ``attempt_globus_data_transfer`` returns
  a ``GlobusTransferResult(status='unconfigured', ...)`` and the
  caller (``cli/setup.py:_step_data``) falls back to the existing
  ``gh release download`` path. Operators who never set up Globus
  see no extra friction.

* Endpoint UUIDs come from env vars (``APECX_GLOBUS_SOURCE_ENDPOINT_ID``,
  ``APECX_GLOBUS_DEST_ENDPOINT_ID``). Hardcoding them in YAML or
  Python would leak per-operator identifiers into the published repo.
  ``.env.example`` documents the convention.

* Transfer items are built here at runtime (not in YAML) because the
  destination paths depend on the operator's chosen ``data_dir``,
  which is a runtime user prompt — not a config-time constant.

Where to look for end-to-end testing
------------------------------------

This module has unit tests with mocked ``globus_sdk`` at
``tests/unit/test_globus_data_transfer.py``. A live end-to-end test
requires (a) ``APECX_GLOBUS_SOURCE_ENDPOINT_ID`` set to a real source
endpoint, (b) the operator's confidential client credentials in the
keyring, and (c) a writable destination endpoint — none of those
can be provided by CI. The live path is exercised by
``apecx-setup data`` itself when an operator has Globus configured.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# The 6 files that constitute the VIOLIN + BV-BRC dataset. Kept here as
# the single source of truth for "what gets transferred"; the same list
# already exists in ``cli/setup_data._EXPECTED_FILES`` but we duplicate
# the literal here to avoid coupling to the gh-release fallback layout
# (the Globus source layout and the tarball-extract layout could in
# principle diverge — the SOURCE path is what's on the Argonne LCF
# collection; the DEST path is what apecx-setup builds locally).
#
# The source paths assume the collection root is the
# ``apecx-joshi-anl-general`` directory referenced in the user's
# directive ("The files location is APECx Data at Argonne LCF
# collection under the path apecx-joshi-anl-general"). Operators with
# a differently-structured source can override the prefix via env var
# ``APECX_GLOBUS_SOURCE_PREFIX``.
_DEFAULT_SOURCE_PREFIX = "/apecx-joshi-anl-general"
_DATASET_FILES = (
    "violin/Vaccine_Information.csv",
    "violin/Pathogen_Information.csv",
    "violin/Gene_Information.csv",
    "violin/Vaccine_Pathogen_Information.csv",
    "violin/Gene_Vaccine_Pathogen_Information.csv",
    "BVBRC_genome_alphavirus.csv",
)


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GlobusPrereqStatus:
    """Why the Globus-first path is or isn't usable on this machine.

    Returned by ``check_globus_prerequisites`` and folded into the
    main ``GlobusTransferResult.detail`` when ``status='unconfigured'``.
    The caller uses ``configured`` to decide whether to bother
    attempting the transfer at all.
    """

    configured: bool
    sdk_installed: bool
    source_endpoint_set: bool
    dest_endpoint_set: bool
    credentials_reachable: bool
    detail: str

    def reason(self) -> str:
        """Human-readable, single-line summary of why we're / aren't
        configured. Safe to print in a CLI status table."""
        if self.configured:
            return "Globus prerequisites OK"
        missing: list[str] = []
        if not self.sdk_installed:
            missing.append("globus_sdk not installed")
        if not self.source_endpoint_set:
            missing.append("APECX_GLOBUS_SOURCE_ENDPOINT_ID unset")
        if not self.dest_endpoint_set:
            missing.append("APECX_GLOBUS_DEST_ENDPOINT_ID unset")
        if not self.credentials_reachable:
            missing.append("no client credentials in env or keyring")
        return "Globus skipped — " + "; ".join(missing) if missing else self.detail


@dataclasses.dataclass(frozen=True)
class GlobusTransferResult:
    """Outcome of an ``attempt_globus_data_transfer`` call.

    ``status`` values:
      * ``"ok"``           — Globus transfer succeeded; ``items_transferred`` set.
      * ``"unconfigured"`` — preconditions missing; caller should fall back.
      * ``"fail"``         — preconditions met but the transfer raised;
                              ``error`` carries the exception's str form.
    """

    status: str
    detail: str
    items_transferred: int = 0
    task_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------


def check_globus_prerequisites() -> GlobusPrereqStatus:
    """Cheap preflight: are all the env vars / SDK / credentials present?

    Does NOT touch the network. Does NOT load the keyring at this
    layer — the keyring read is deferred to GlobusTransferStep itself
    (G23 ``build_globus_app`` handles it). What we check here is the
    necessary-but-not-sufficient set so the caller can short-circuit
    without paying any network cost.
    """
    # 1. globus_sdk importability.
    sdk_installed = True
    try:
        import globus_sdk  # noqa: F401, PLC0415 — preflight probe
    except ImportError:
        sdk_installed = False

    # 2. Endpoint UUIDs.
    source_endpoint = os.environ.get("APECX_GLOBUS_SOURCE_ENDPOINT_ID", "").strip()
    dest_endpoint = os.environ.get("APECX_GLOBUS_DEST_ENDPOINT_ID", "").strip()
    source_set = bool(source_endpoint)
    dest_set = bool(dest_endpoint)

    # 3. Credentials reachable. THREE valid paths (G90, 2026-05-16):
    #
    #   a. Confidential client via env vars
    #      ($GLOBUS_COMPUTE_CLIENT_ID + $GLOBUS_COMPUTE_CLIENT_SECRET).
    #      CI / container default.
    #   b. Confidential client via OS keyring (apecx-globus-setup store).
    #      Standard workstation install.
    #   c. Native client via persisted tokens
    #      ($APECX_GLOBUS_NATIVE_CLIENT_ID set + a prior
    #      `apecx-globus-setup login --client-id <UUID>`).
    #      Dev / interactive path.
    #
    # We don't probe the on-disk JSON token file directly — that's
    # globus_sdk's territory; it knows how to detect expired/missing
    # tokens and re-prompt. The presence of $APECX_GLOBUS_NATIVE_CLIENT_ID
    # is the contract that the operator has set up the native path
    # (whether the tokens are still valid is checked at call time).
    credentials_reachable = bool(
        (
            os.environ.get("GLOBUS_COMPUTE_CLIENT_ID")
            and os.environ.get("GLOBUS_COMPUTE_CLIENT_SECRET")
        )
        or _keyring_credentials_present()
        or os.environ.get("APECX_GLOBUS_NATIVE_CLIENT_ID")
    )

    configured = sdk_installed and source_set and dest_set and credentials_reachable

    detail = (
        "Globus prerequisites OK"
        if configured
        else "Globus prerequisites incomplete — see reason()"
    )
    return GlobusPrereqStatus(
        configured=configured,
        sdk_installed=sdk_installed,
        source_endpoint_set=source_set,
        dest_endpoint_set=dest_set,
        credentials_reachable=credentials_reachable,
        detail=detail,
    )


def _keyring_credentials_present() -> bool:
    """True iff apecx-globus-setup has stored credentials in the OS
    keyring. Cheap: one keyring lookup, no network. False on every
    failure mode (keyring missing, no entries, etc.) — we never raise
    out of a preflight check."""
    try:
        import keyring  # noqa: PLC0415

        client_id = keyring.get_password("apecx-globus-setup", "client_id")
        client_secret = keyring.get_password("apecx-globus-setup", "client_secret")
        return bool(client_id and client_secret)
    except Exception:  # noqa: BLE001 — preflight, must never raise
        return False


# ---------------------------------------------------------------------------
# Transfer items builder
# ---------------------------------------------------------------------------


def build_transfer_items(data_dir: Path) -> list[dict[str, str]]:
    """Build the {source_path, dest_path} list for the 6 dataset files.

    The source prefix is read from ``APECX_GLOBUS_SOURCE_PREFIX``
    (defaults to ``/apecx-joshi-anl-general`` per the user's directive).
    Destination paths are anchored at the operator's chosen
    ``data_dir``.

    Items are returned in stable order (matching ``_DATASET_FILES``)
    so retries always hit the same Globus task layout and log lines
    stay diff-friendly.
    """
    source_prefix = os.environ.get(
        "APECX_GLOBUS_SOURCE_PREFIX",
        _DEFAULT_SOURCE_PREFIX,
    ).rstrip("/")
    return [
        {
            "source_path": f"{source_prefix}/{relpath}",
            "dest_path": str(data_dir / relpath),
        }
        for relpath in _DATASET_FILES
    ]


# ---------------------------------------------------------------------------
# Wrapper-YAML resolution
# ---------------------------------------------------------------------------


def _wrapper_yaml_path() -> Path:
    """Resolve the wrapper YAML's filesystem path.

    The YAML ships **in this repo** at
    ``configs/globus_transfers/violin_bvbrc_transfer_step.yml``,
    so we anchor on this module's own location rather than
    ``locate_workflow_root`` (which returns the workspace parent
    and would land in the wrong tree under a git worktree checkout
    like ``wt-cgu-codegen-uplift/``).

    Layout assumed:
      ``<repo_root>/src/apecx_integration/cli/_globus_data_transfer.py``
      ``<repo_root>/configs/globus_transfers/violin_bvbrc_transfer_step.yml``

    Operators who install the package via ``pip install`` (no
    editable mode, no source tree) won't have the ``configs/``
    directory beside the installed package. In that case the YAML
    file is genuinely absent — ``attempt_globus_data_transfer``
    returns status='fail' and the caller falls back to gh release.
    That's the right failure mode: the operator can clone the repo
    if they want the Globus path.
    """
    # __file__ = <repo>/src/apecx_integration/cli/_globus_data_transfer.py
    # parents[3] = <repo>
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "configs" / "globus_transfers" / "violin_bvbrc_transfer_step.yml"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def attempt_globus_data_transfer(
    data_dir: Path,
    *,
    poll_timeout_seconds: float = 600.0,
) -> GlobusTransferResult:
    """Try to transfer the dataset via Globus. Return a structured result.

    Status flow:
      * preconditions not met → ``GlobusTransferResult(status='unconfigured')``;
        caller falls back to gh-release download.
      * preconditions met → load the wrapper YAML, build items, drive
        ``GlobusTransferStep.process``. On success → status='ok'.
        On any exception → status='fail' with ``error`` set.

    This function is **synchronous** despite calling an async step:
    it wraps the await in ``asyncio.run`` because the caller
    (``cli/setup.py:_step_data``) is sync code (an apecx-setup
    subcommand handler). Tests that already own a running event loop
    can drive ``_attempt_globus_data_transfer_async`` directly.
    """
    prereqs = check_globus_prerequisites()
    if not prereqs.configured:
        log.info("Globus path skipped: %s", prereqs.reason())
        return GlobusTransferResult(
            status="unconfigured",
            detail=prereqs.reason(),
        )

    try:
        return asyncio.run(
            _attempt_globus_data_transfer_async(
                data_dir=data_dir,
                poll_timeout_seconds=poll_timeout_seconds,
            )
        )
    except RuntimeError as exc:
        # asyncio.run raises RuntimeError if there's already a running
        # loop. The cli/setup.py caller never has one, but a future
        # async caller might — surface the actionable error.
        if "asyncio.run() cannot be called from a running event loop" in str(exc):
            log.error(
                "attempt_globus_data_transfer called from inside a "
                "running event loop. Call _attempt_globus_data_transfer_async "
                "directly with `await`."
            )
            raise
        return GlobusTransferResult(
            status="fail",
            detail=f"unexpected RuntimeError: {exc}",
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — we want to capture EVERY failure mode
        return GlobusTransferResult(
            status="fail",
            detail=f"{type(exc).__name__}: {exc}",
            error=str(exc),
        )


async def _attempt_globus_data_transfer_async(
    *,
    data_dir: Path,
    poll_timeout_seconds: float,
) -> GlobusTransferResult:
    """Async core. Materialize step from YAML, drive process()."""
    from nanobrain.library.steps.globus_transfer_step import GlobusTransferStep

    wrapper_yaml = _wrapper_yaml_path()
    if not wrapper_yaml.is_file():
        return GlobusTransferResult(
            status="fail",
            detail=(
                f"wrapper YAML missing at {wrapper_yaml}. Reinstall "
                "apecx-mcp-integration to restore it, or report this as a bug."
            ),
        )

    step = GlobusTransferStep.from_config(wrapper_yaml)
    items = build_transfer_items(data_dir)

    log.info(
        "Globus transfer: %d items, source=%s, dest=%s",
        len(items),
        items[0]["source_path"].split("/")[1] if items else "(none)",
        data_dir,
    )

    # Ensure the destination subdirectories exist locally — Globus
    # Connect Personal writes to the dest path verbatim and won't
    # create intermediate dirs.
    for item in items:
        Path(item["dest_path"]).parent.mkdir(parents=True, exist_ok=True)

    out = await step.process({"items": items})
    return GlobusTransferResult(
        status="ok",
        detail=f"transferred {out.get('items_transferred', len(items))} items",
        items_transferred=int(out.get("items_transferred", len(items))),
        task_id=out.get("task_id"),
    )


__all__ = [
    "GlobusPrereqStatus",
    "GlobusTransferResult",
    "attempt_globus_data_transfer",
    "build_transfer_items",
    "check_globus_prerequisites",
]
