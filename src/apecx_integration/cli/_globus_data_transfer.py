"""Apecx-side glue for the Globus data transfer (G82 2026-05-16; G127 2026-05-21).

Globus is the SOLE data-acquisition path as of 2026-05-21 — the legacy
``gh release download`` fallback was retired. This module:

  1. Checks operator preconditions (globus_sdk installed, endpoint UUIDs
     set, credentials reachable) and reports them as a ``GlobusPrereqStatus``.
  2. Builds the runtime ``items`` payload — the 5 VIOLIN CSVs + the curated
     BV-BRC CSV — from independently-overridable source roots (see
     ``build_transfer_items``), with dest paths under the operator's chosen
     local data directory.
  3. Loads the verify→transfer nanobrain workflow
     (``configs/globus_transfers/violin_bvbrc_transfer_workflow.yml``) and
     drives it via ``Workflow.run``. The workflow gates the transfer behind
     ``GlobusManifestVerifyStep`` (fail-loud source-existence check) wired to
     ``GlobusTransferStep`` by a ``DirectLink``.

The two step YAMLs + the workflow YAML are the framework-native configs; the
Python here is a thin orchestrator that picks the right inputs and surfaces a
structured result to ``cli/setup.py:_step_data``.

Brutal-truth design notes
-------------------------

* Globus is REQUIRED. When preconditions are unmet,
  ``attempt_globus_data_transfer`` returns ``GlobusTransferResult(
  status='unconfigured', ...)`` and ``cli/setup.py:_step_data`` FAILS LOUD with
  actionable setup instructions (unless the dataset is already present locally).
  There is no silent degradation — that is the whole point of retiring gh.

* ``Workflow.run`` SWALLOWS a step exception (it returns ``status='completed'``
  with empty outputs even when the verify step raised). Trusting that status
  would be a silent failure. The ONLY success signal trusted here is
  ``transfer_status == 'SUCCEEDED'``; any other outcome is surfaced FAIL-LOUD
  using the captured ``step_failed`` event's exception message.

* Endpoint UUIDs come from env vars (``APECX_GLOBUS_SOURCE_ENDPOINT_ID``,
  ``APECX_GLOBUS_DEST_ENDPOINT_ID``). Hardcoding them would leak per-operator
  identifiers into the published repo. ``.env.example`` documents the convention.

* Transfer items are built here at runtime (not in YAML) because the
  destination paths depend on the operator's chosen ``data_dir`` (a runtime
  prompt), not a config-time constant.

Where to look for end-to-end testing
------------------------------------

Unit tests: ``tests/unit/test_globus_data_transfer.py`` (logic + workflow load
+ WorkflowBuilder parity). Gated live tests:
``tests/integration/test_globus_transfer_live.py`` — the missing-source gate
runs against real source auth; the full transfer needs a real writable dest
endpoint (Globus Connect Personal) and ``APECX_GLOBUS_LIVE_TRANSFER=1``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# The 6 files that constitute the VIOLIN + BV-BRC dataset, and WHERE they live
# on the "APECx Data at Argonne LCF" collection. Single source of truth for
# "what gets transferred".
#
# Source layout — BOTH live-verified on collection 8d2e71d6 ("APECx Data at
# Argonne LCF"), 2026-05-21. VIOLIN and BV-BRC live under DIFFERENT parent dirs
# of the SAME collection, so each has its own env-overridable source root (one
# endpoint, two roots — no per-dataset endpoint needed):
#
#   BV-BRC  — /apecx-ramanathan-anl/public/data/BV-BRC/
#               BVBRC_genome_alphavirus.csv   (12 MB)        ✅ public path
#
#   VIOLIN  — /apecx-ramanathan-anl/apecx-project-all/violin/
#               Vaccine_Information.csv (1.6 MB), Pathogen_Information.csv,
#               Gene_Information.csv, Vaccine_Pathogen_Information.csv,
#               Gene_Vaccine_Pathogen_Information.csv
#               (+ VIOLIN_Curated_References.txt — present but NOT transferred;
#                not in _EXPECTED_FILES / not read downstream)
#             ✅ ACL-gated by the `apecx-project-all` Globus Group. The earlier
#                "500 Path not allowed" was that ACL gate (it presents as a path
#                restriction on a GCSv5 guest collection), NOT a separate
#                collection. Group membership for the transfer identity
#                (bbcdba6f-...@clients.auth.globus.org) unlocked the SAME path on
#                the SAME endpoint — verified 2026-05-21 once the grant landed.
#                Operators whose identity is NOT in the Group still get a clean
#                install: VIOLIN is OPTIONAL (warn + 'partial'), see
#                cli/setup.py:_step_data.
#
# Safety: the verify->transfer workflow runs GlobusManifestVerifyStep (G127)
# FIRST. A wrong VIOLIN path FAILS LOUD naming every missing file — never a
# silent zero/partial transfer.
#
# The content-divergence hack is GONE: the BV-BRC source is the genuinely
# alphavirus-curated 12 MB file, not the ~1.5 GB all-genomes file renamed at the
# dest. Source content == what downstream apecx_db_integration expects.
#
# Dest layout is unchanged (``violin/<File>.csv`` + top-level
# ``BVBRC_genome_alphavirus.csv``) so it matches ``_EXPECTED_FILES``.
_DEFAULT_BVBRC_SOURCE_DIR = "/apecx-ramanathan-anl/public/data/BV-BRC"
_DEFAULT_VIOLIN_SOURCE_DIR = "/apecx-ramanathan-anl/apecx-project-all/violin"

_VIOLIN_FILES = (
    "Vaccine_Information.csv",
    "Pathogen_Information.csv",
    "Gene_Information.csv",
    "Vaccine_Pathogen_Information.csv",
    "Gene_Vaccine_Pathogen_Information.csv",
)
_BVBRC_FILE = "BVBRC_genome_alphavirus.csv"

# Dataset partition (2026-05-21). BV-BRC is on the PUBLIC collection (verified,
# always reachable with the M2M creds) → REQUIRED. VIOLIN is on a Group-gated
# collection (`apecx-project-all`) the transfer identity is not yet a member of
# → OPTIONAL until that membership is granted. The CLI (`cli/setup.py:_step_data`)
# transfers REQUIRED must-succeed and OPTIONAL warn-on-fail, so a clean install
# completes (with a loud VIOLIN-missing warning) on public data alone.
REQUIRED_DATASETS = ("bvbrc",)
OPTIONAL_DATASETS = ("violin",)
_ALL_DATASETS = ("violin", "bvbrc")


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
    """True iff confidential-client credentials are in the OS keyring.

    Delegates to nanobrain's ``globus_credentials.load_credentials`` — the
    SINGLE source of truth for where the credential pair lives (service
    ``nanobrain-globus``). This MUST match what ``build_globus_app``'s
    tier-3 keyring lookup reads; an earlier copy hardcoded the wrong
    service name (``apecx-globus-setup``), so this preflight reported
    "not configured" even when ``apecx-globus-setup store`` had written
    valid creds and ``build_globus_app`` would have found them. With the
    gh fallback removed that disagreement turns a working setup into a
    hard install failure. Cheap: one keyring lookup, no network. False on
    every failure mode (keyring missing, no entries, etc.) — a preflight
    check never raises.
    """
    try:
        from nanobrain.core.distributed import globus_credentials  # noqa: PLC0415

        client_id, client_secret = globus_credentials.load_credentials()
        return bool(client_id and client_secret)
    except Exception:  # noqa: BLE001 — preflight, must never raise
        return False


# ---------------------------------------------------------------------------
# Transfer items builder
# ---------------------------------------------------------------------------


def build_transfer_items(
    data_dir: Path, *, datasets: tuple[str, ...] | set[str] | None = None
) -> list[dict[str, str]]:
    """Build the {source_path, dest_path} list for the dataset files.

    ``datasets`` selects which dataset groups to include — a subset of
    ``{"violin", "bvbrc"}``. ``None`` (default) includes BOTH (the full 6-file
    manifest; keeps backward-compatible behavior). Pass ``{"bvbrc"}`` or
    ``{"violin"}`` to build a single-dataset manifest — the CLI uses this to
    transfer REQUIRED (BV-BRC) and OPTIONAL (VIOLIN) groups independently so a
    VIOLIN failure doesn't abort the public-data install.

    Source layout (re-mapped 2026-05-21 — VIOLIN and BV-BRC live under
    DIFFERENT parents, so each has its own env-overridable root):
        ``$APECX_GLOBUS_VIOLIN_SOURCE_DIR/<5 VIOLIN CSVs>``
        ``$APECX_GLOBUS_BVBRC_SOURCE_DIR/BVBRC_genome_alphavirus.csv``

    Dest layout (unchanged — matches ``_EXPECTED_FILES``):
        ``$data_dir/violin/*.csv`` + ``$data_dir/BVBRC_genome_alphavirus.csv``

    VIOLIN files are listed first, BV-BRC last, so order is stable across
    retries and log lines stay diff-friendly.
    """
    include = set(datasets) if datasets is not None else set(_ALL_DATASETS)
    unknown = include - set(_ALL_DATASETS)
    if unknown:
        raise ValueError(
            f"build_transfer_items: unknown dataset(s) {sorted(unknown)}; "
            f"valid: {sorted(_ALL_DATASETS)}"
        )

    items: list[dict[str, str]] = []
    if "violin" in include:
        violin_root = os.environ.get(
            "APECX_GLOBUS_VIOLIN_SOURCE_DIR", _DEFAULT_VIOLIN_SOURCE_DIR
        ).rstrip("/")
        items += [
            {
                "source_path": f"{violin_root}/{fn}",
                "dest_path": str(data_dir / "violin" / fn),
            }
            for fn in _VIOLIN_FILES
        ]
    if "bvbrc" in include:
        bvbrc_root = os.environ.get(
            "APECX_GLOBUS_BVBRC_SOURCE_DIR", _DEFAULT_BVBRC_SOURCE_DIR
        ).rstrip("/")
        items.append(
            {
                "source_path": f"{bvbrc_root}/{_BVBRC_FILE}",
                "dest_path": str(data_dir / _BVBRC_FILE),
            }
        )
    return items


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


def _workflow_yaml_path() -> Path:
    """Resolve the verify->transfer workflow YAML (G127).

    Same repo-anchored resolution as ``_wrapper_yaml_path``. This is the
    framework-native drive path: a two-step nanobrain workflow that gates the
    transfer behind ``GlobusManifestVerifyStep`` (fail-loud source existence
    check). The two step configs it references live beside it in
    ``configs/globus_transfers/``.
    """
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "configs" / "globus_transfers" / "violin_bvbrc_transfer_workflow.yml"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def attempt_globus_data_transfer(
    data_dir: Path,
    *,
    poll_timeout_seconds: float = 600.0,
    datasets: tuple[str, ...] | set[str] | None = None,
) -> GlobusTransferResult:
    """Try to transfer the dataset(s) via Globus. Return a structured result.

    ``datasets`` (subset of ``{"violin", "bvbrc"}``; ``None`` = both) scopes the
    transfer so the CLI can run REQUIRED (BV-BRC) and OPTIONAL (VIOLIN) groups
    independently — a VIOLIN failure then doesn't abort the public-data install.

    Status flow:
      * preconditions not met → ``GlobusTransferResult(status='unconfigured')``.
      * preconditions met → load the verify→transfer workflow, build items for
        the requested datasets, drive ``Workflow.run``. Success (all reached
        SUCCEEDED) → status='ok'; any failure → status='fail' with ``error`` set.

    This function is **synchronous** despite driving an async workflow: it wraps
    the await in ``asyncio.run`` because the caller (``cli/setup.py:_step_data``)
    is sync code. Tests with a running loop can await
    ``_attempt_globus_data_transfer_async`` directly.
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
                datasets=datasets,
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


def _resolve_auth_env() -> str:
    """Populate ``APECX_GLOBUS_RESOLVED_*`` env vars based on auth_mode.

    The wrapper YAML reads only the *RESOLVED* env vars (single source
    of truth — no nested interpolation, which nanobrain doesn't support).
    This function maps the operator's actual config (confidential creds
    OR native client_id) into the resolved slot the YAML reads.

    Returns the auth_mode that was resolved ("native" or "client_credentials").

    Precedence:
      * Confidential creds present in env → client_credentials
      * Native client_id present in env → native
      * Operator-set ``APECX_GLOBUS_AUTH_MODE`` overrides the auto-pick
        (useful for testing the OTHER path when both are configured).
    """
    explicit_mode = os.environ.get("APECX_GLOBUS_AUTH_MODE", "").strip().lower()
    have_confidential = bool(
        os.environ.get("GLOBUS_COMPUTE_CLIENT_ID")
        and os.environ.get("GLOBUS_COMPUTE_CLIENT_SECRET")
    )
    have_native = bool(os.environ.get("APECX_GLOBUS_NATIVE_CLIENT_ID"))

    if explicit_mode == "native" and have_native:
        mode = "native"
    elif explicit_mode == "client_credentials" and have_confidential or have_confidential:
        mode = "client_credentials"
    elif have_native:
        mode = "native"
    else:
        # Neither path is configured. _resolve_auth_env still resolves
        # to client_credentials so the YAML loads, but the downstream
        # GlobusTransferStep auth will fail-loud (intended — we don't
        # mask the missing-config state).
        mode = "client_credentials"

    if mode == "native":
        os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] = os.environ["APECX_GLOBUS_NATIVE_CLIENT_ID"]
        os.environ["APECX_GLOBUS_RESOLVED_CLIENT_SECRET"] = ""
        os.environ["APECX_GLOBUS_AUTH_MODE"] = "native"
    else:  # client_credentials
        os.environ["APECX_GLOBUS_RESOLVED_CLIENT_ID"] = os.environ.get(
            "GLOBUS_COMPUTE_CLIENT_ID", ""
        )
        os.environ["APECX_GLOBUS_RESOLVED_CLIENT_SECRET"] = os.environ.get(
            "GLOBUS_COMPUTE_CLIENT_SECRET", ""
        )
        os.environ["APECX_GLOBUS_AUTH_MODE"] = "client_credentials"

    return mode


async def _attempt_globus_data_transfer_async(
    *,
    data_dir: Path,
    poll_timeout_seconds: float,
    datasets: tuple[str, ...] | set[str] | None = None,
) -> GlobusTransferResult:
    """Async core. Drive the verify->transfer nanobrain workflow (G127).

    Framework-native: instead of calling ``GlobusTransferStep.process`` directly,
    this loads ``violin_bvbrc_transfer_workflow.yml`` and drives it via
    ``Workflow.run``. The workflow gates the transfer behind
    ``GlobusManifestVerifyStep`` — a fail-loud source-existence check wired by a
    data-dependency link, so the transfer can never run on a missing source.

    Critical honesty contract: ``Workflow.run`` SWALLOWS a step exception (it
    returns ``status: 'completed'`` with empty outputs even when the verify step
    raised). Trusting that ``status`` would be a silent failure — "driver reports
    ok, zero files moved". The ONLY success signal we trust is
    ``transfer_status == 'SUCCEEDED'`` (the transfer step's terminal status,
    propagated to a workflow output). Any other outcome is surfaced FAIL-LOUD,
    using the captured ``step_failed`` event's exception message (e.g. the exact
    list of missing source files from the verify gate).
    """
    from nanobrain.core.step_events import subscribe_to_step_events
    from nanobrain.core.workflow import Workflow

    workflow_yaml = _workflow_yaml_path()
    if not workflow_yaml.is_file():
        return GlobusTransferResult(
            status="fail",
            detail=(
                f"workflow YAML missing at {workflow_yaml}. Reinstall "
                "apecx-mcp-integration to restore it, or report this as a bug."
            ),
        )

    # G90 (2026-05-17): resolve which auth path to use BEFORE loading the YAML.
    # Both step configs read only ${APECX_GLOBUS_RESOLVED_CLIENT_ID/SECRET}.
    auth_mode = _resolve_auth_env()
    log.info("Globus auth_mode resolved to: %s", auth_mode)

    items = build_transfer_items(data_dir, datasets=datasets)
    if not items:
        return GlobusTransferResult(
            status="fail",
            detail=f"no transfer items for datasets={datasets!r}",
        )
    log.info(
        "Globus verify->transfer workflow: %d items, source=%s, dest=%s",
        len(items),
        items[0]["source_path"].split("/")[1] if items else "(none)",
        data_dir,
    )

    # Ensure the destination subdirectories exist on the LOCAL filesystem.
    # Globus Connect Personal writes to the dest path verbatim and won't create
    # intermediate dirs — passing ``/Users/.../violin/X.csv`` without ``violin/``
    # existing fails per-file with "no such file or directory". Skip the mkdir
    # for ``/~/`` shorthand (resolves to the dest endpoint's home dir, not a
    # literal ``/~`` locally).
    for item in items:
        dest_path_str = item["dest_path"]
        if dest_path_str.startswith("/~/") or dest_path_str.startswith("~/"):
            continue
        Path(dest_path_str).parent.mkdir(parents=True, exist_ok=True)

    wf = Workflow.from_config(workflow_yaml)

    # The transfer step's own poll_timeout_seconds (YAML) governs the Globus
    # poll; the workflow wall-clock timeout must comfortably exceed it.
    step_poll = float(wf.child_steps["transfer"].transfer_config.poll_timeout_seconds)
    wf_timeout = max(step_poll, poll_timeout_seconds) + 120.0

    # Capture step_failed events so a swallowed cascade exception (e.g. the
    # verify gate) becomes an actionable apecx-side failure message.
    failures: list[tuple[str, str, str]] = []

    def _capture(event) -> None:
        if getattr(event, "event_type", None) == "step_failed":
            exc = (getattr(event, "payload", None) or {}).get("exception", {})
            failures.append((event.step_name, exc.get("type", ""), exc.get("message", "")))

    with subscribe_to_step_events(_capture):
        outputs = await wf.run(
            {"workflow_input": {"items": items}},
            timeout=wf_timeout,
            settle_ms=500,
            raise_on_cascade_timeout=False,
        )

    if not isinstance(outputs, dict):
        return GlobusTransferResult(
            status="fail",
            detail=f"workflow returned non-dict: {type(outputs).__name__}",
        )
    if outputs.get("status") == "cascade_timeout":
        return GlobusTransferResult(
            status="fail",
            detail=f"workflow cascade timed out after {wf_timeout:.0f}s",
        )

    # The ONLY trusted success signal — see the docstring's honesty contract.
    if outputs.get("transfer_status") == "SUCCEEDED":
        count = outputs.get("transfer_items_count")
        count = int(count) if count is not None else len(items)
        return GlobusTransferResult(
            status="ok",
            detail=f"verify->transfer workflow transferred {count} items",
            items_transferred=count,
            task_id=outputs.get("transfer_task_id"),
        )

    # Not SUCCEEDED → a step failed (or the transfer never ran). Surface the
    # captured failure FAIL-LOUD rather than masking it as success.
    if failures:
        step_name, exc_type, exc_msg = failures[0]
        return GlobusTransferResult(
            status="fail",
            detail=f"workflow step {step_name!r} failed ({exc_type}): {exc_msg}",
            error=exc_msg,
        )
    return GlobusTransferResult(
        status="fail",
        detail=(
            "transfer did not reach SUCCEEDED "
            f"(transfer_status={outputs.get('transfer_status')!r}) and no "
            "step_failed event was captured"
        ),
    )


__all__ = [
    "GlobusPrereqStatus",
    "GlobusTransferResult",
    "attempt_globus_data_transfer",
    "build_transfer_items",
    "check_globus_prerequisites",
]
