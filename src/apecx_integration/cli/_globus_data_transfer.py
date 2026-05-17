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
# the single source of truth for "what gets transferred".
#
# The actual layout on the "APECx Data at Argonne LCF" Globus collection
# (verified 2026-05-17 via live LIST):
#
#   /apecx-joshi-anl-general/
#     2024_12_17_VIOLIN/         ← VIOLIN CSVs
#       Vaccine_Information.csv
#       Pathogen_Information.csv
#       Gene_Information.csv
#       Vaccine_Pathogen_Information.csv
#       Gene_Vaccine_Pathogen_Information.csv
#       2025_04_28_VIOLIN_Web_Portal_Data_UML.{html,png}  ← skipped
#     2025_05_05_BVBRC/          ← BV-BRC CSVs
#       BVBRC_epitope__epitopes.csv
#       BVBRC_genome.csv         ← closest analog to legacy BVBRC_genome_alphavirus.csv
#       BVBRC_genome_feature.csv
#       BVBRC_protein_feature__domains_and_motifs.csv
#       BVBRC_protein_structure__protein_structures.csv
#     2025_06_25_ProtaBank/
#     2025_11_05_PubMed/
#     2025_11_17_IEDB/
#     2025_11_18_PDB/
#
# The DEST layout this module produces matches what ``cli/setup_data``
# extracts from the gh-release tarball (``violin/<File>.csv`` +
# top-level ``BVBRC_genome_alphavirus.csv``), so downstream code that
# already reads those paths (``apecx_db_integration``, etc.) needs
# no changes regardless of whether the data arrived via Globus or gh.
#
# Two date-stamped directory names are env-overridable so operators
# pulling from a newer snapshot don't need a code change:
#   * APECX_GLOBUS_VIOLIN_DIR (default: 2024_12_17_VIOLIN)
#   * APECX_GLOBUS_BVBRC_DIR  (default: 2025_05_05_BVBRC)
_DEFAULT_SOURCE_PREFIX = "/apecx-joshi-anl-general"
_DEFAULT_VIOLIN_DIR = "2024_12_17_VIOLIN"
_DEFAULT_BVBRC_DIR = "2025_05_05_BVBRC"

# Tuples of (source_relpath, dest_relpath). The source side has the
# date-stamped layout; the dest side has the legacy flat layout the
# rest of the codebase expects.
_DATASET_FILE_MAPPING = (
    ("{VIOLIN}/Vaccine_Information.csv", "violin/Vaccine_Information.csv"),
    ("{VIOLIN}/Pathogen_Information.csv", "violin/Pathogen_Information.csv"),
    ("{VIOLIN}/Gene_Information.csv", "violin/Gene_Information.csv"),
    ("{VIOLIN}/Vaccine_Pathogen_Information.csv", "violin/Vaccine_Pathogen_Information.csv"),
    (
        "{VIOLIN}/Gene_Vaccine_Pathogen_Information.csv",
        "violin/Gene_Vaccine_Pathogen_Information.csv",
    ),
    # ⚠️ BV-BRC content divergence (CRITICAL):
    #
    #   Source FILE on Globus: BVBRC_genome.csv (~1.5 GB, ALL genomes)
    #   Dest FILE on disk:     BVBRC_genome_alphavirus.csv (filename
    #                          matches the legacy gh-release tarball
    #                          so downstream code keeps working)
    #
    # The rename is for filename-only backwards compatibility — the
    # CONTENT is different:
    #
    #   * Legacy (gh release):     ~MB-scale, alphavirus-curated subset
    #   * Modern (Globus source):  ~1.5 GB, ALL BV-BRC genomes (every virus,
    #                              not just alphavirus)
    #
    # Downstream code in ``apecx_db_integration`` that reads this file
    # treating it as alphavirus-only will:
    #   * Load way more data than expected (1.5 GB pandas DataFrame)
    #   * Match queries against the full genome catalog, not just
    #     alphavirus
    #   * Produce broader (potentially noisier) match results
    #
    # If you need alphavirus-only, post-filter in code:
    #     df[df['Genus'] == 'Alphavirus']
    # ...or continue using ``apecx-setup --prefer-gh-release`` which
    # fetches the curated subset from the gh-release tarball.
    ("{BVBRC}/BVBRC_genome.csv", "BVBRC_genome_alphavirus.csv"),
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

    Source layout (post-G91 2026-05-17):
        ``$APECX_GLOBUS_SOURCE_PREFIX/$APECX_GLOBUS_VIOLIN_DIR/*.csv``
        ``$APECX_GLOBUS_SOURCE_PREFIX/$APECX_GLOBUS_BVBRC_DIR/BVBRC_genome.csv``

    Dest layout (unchanged — matches gh-release tarball):
        ``$data_dir/violin/*.csv``
        ``$data_dir/BVBRC_genome_alphavirus.csv``

    All three source-side path components are env-overridable so
    operators pulling from a newer snapshot don't need a code change:
        * APECX_GLOBUS_SOURCE_PREFIX (default: /apecx-joshi-anl-general)
        * APECX_GLOBUS_VIOLIN_DIR    (default: 2024_12_17_VIOLIN)
        * APECX_GLOBUS_BVBRC_DIR     (default: 2025_05_05_BVBRC)

    Items are returned in stable order (matching ``_DATASET_FILE_MAPPING``)
    so retries always hit the same Globus task layout and log lines
    stay diff-friendly.
    """
    source_prefix = os.environ.get(
        "APECX_GLOBUS_SOURCE_PREFIX",
        _DEFAULT_SOURCE_PREFIX,
    ).rstrip("/")
    violin_dir = os.environ.get("APECX_GLOBUS_VIOLIN_DIR", _DEFAULT_VIOLIN_DIR)
    bvbrc_dir = os.environ.get("APECX_GLOBUS_BVBRC_DIR", _DEFAULT_BVBRC_DIR)

    items: list[dict[str, str]] = []
    for src_template, dest_relpath in _DATASET_FILE_MAPPING:
        src_relpath = src_template.format(VIOLIN=violin_dir, BVBRC=bvbrc_dir)
        items.append(
            {
                "source_path": f"{source_prefix}/{src_relpath}",
                "dest_path": str(data_dir / dest_relpath),
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

    # G90 (2026-05-17): resolve which auth path to use BEFORE loading
    # the YAML. The YAML reads only ${APECX_GLOBUS_RESOLVED_CLIENT_ID/SECRET}
    # — this function maps confidential or native config into that slot.
    auth_mode = _resolve_auth_env()
    log.info("Globus auth_mode resolved to: %s", auth_mode)

    step = GlobusTransferStep.from_config(wrapper_yaml)
    items = build_transfer_items(data_dir)

    log.info(
        "Globus transfer: %d items, source=%s, dest=%s",
        len(items),
        items[0]["source_path"].split("/")[1] if items else "(none)",
        data_dir,
    )

    # Ensure the destination subdirectories exist on the LOCAL
    # filesystem. Globus Connect Personal writes to the dest path
    # verbatim and won't create intermediate dirs — if we pass
    # ``/Users/.../violin/X.csv`` without ``violin/`` existing, the
    # transfer fails per-file with "no such file or directory."
    #
    # Skip this mkdir step when the operator passed a Globus-side
    # path with ``/~/`` shorthand (which resolves to the home dir on
    # the dest endpoint, NOT a literal ``/~`` on the local filesystem).
    # In that case Globus Connect Personal will resolve ``/~/`` itself
    # and create intermediate dirs as part of the transfer; the
    # local-filesystem mkdir would either fail (``/~`` is read-only
    # under macOS's root namespace) or create a misleading literal
    # ``~`` directory.
    for item in items:
        dest_path_str = item["dest_path"]
        if dest_path_str.startswith("/~/") or dest_path_str.startswith("~/"):
            continue
        Path(dest_path_str).parent.mkdir(parents=True, exist_ok=True)

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
