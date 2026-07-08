"""Auto-discover the Rhea checkout + set RHEA_* env vars (G88, 2026-05-16).

Run by ``apecx-mcp`` BEFORE ``InfraOrchestrator.start_all()``. Closes the
operator-friction gap where the orchestrator's existing rhea_mcp
auto-spawn logic only engages if the operator has manually exported
``RHEA_REPO_PATH`` + ``RHEA_PYTHON_PATH``.

What this module does
---------------------

For each env var the orchestrator's rhea spec relies on, this module:

1. **Honors any value the operator has already set.** Auto-discovery
   NEVER overwrites an explicit env var. The operator stays in
   control.
2. **Probes a known search order** for the missing value.
3. **Applies platform-aware defaults** for macOS (the host where the
   rhea-server lifecycle has the most knobs — Parsl backend,
   conda-env extraction dir).

Env vars handled
----------------

* ``RHEA_REPO_PATH`` — the Rhea source checkout. Search order:
    1. Sibling of the apecx-mcp-integration repo (``<apecx>/../rhea``)
    2. Common dev locations: ``~/src/rhea``, ``~/code/rhea``,
       ``~/Downloads/apecx-cowork/rhea``
    3. Any directory matching ``rhea`` under the workspace root
       (G40-resolved)
* ``RHEA_PYTHON_PATH`` — derived as ``$RHEA_REPO_PATH/.venv/bin``
  when that path exists and contains a ``python`` binary.
* ``RHEA_CONDA_ENVS_DIR`` — derived as ``$TMPDIR/apecx-rhea/conda/envs``
  on macOS (where ``/home/rhea`` is read-only autofs and the rhea
  agent's default unpack target fails). Linux operators get rhea's
  in-tree default (``/home/rhea/conda/envs``) unless they override.
* ``PARSL_CONTAINER_BACKEND`` — defaults to ``local`` on macOS for
  the rhea agent (per the documented constraint in rhea/manager/
  parsl_config.py — see the May findings doc). Linux operators get
  rhea's own default unless they override.

Honest scope
------------

This module **does not** build the rhea-server image, seed the tool
catalog, or pull the embedding model. Those are handled by the
``InfraOrchestrator`` at startup (the container auto-builds from local
rhea source, and ``ensure_catalog_seeded`` pulls the embedding model +
runs the ingestion when the catalog is empty). This module is the cheap
discovery layer that makes that orchestrator engage without operator
intervention by resolving the ``RHEA_*`` env vars from the source checkout.

If the slow setup has NOT been done, the orchestrator's existing
probes fail loud with their existing actionable error messages —
exactly what should happen.
"""

from __future__ import annotations

import logging
import os
import platform
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Env vars this module manages. The names match the orchestrator's
# ``_RHEA_*`` constants — kept as string literals here to avoid
# importing the orchestrator at apecx-mcp startup (circular concern;
# the orchestrator is imported lazily later).
_RHEA_REPO_PATH = "RHEA_REPO_PATH"
_RHEA_PYTHON_PATH = "RHEA_PYTHON_PATH"
_RHEA_CONDA_ENVS_DIR = "RHEA_CONDA_ENVS_DIR"
_PARSL_CONTAINER_BACKEND = "PARSL_CONTAINER_BACKEND"
_RHEA_MCP_URL = "RHEA_MCP_URL"
# The rhea-server always runs as a container published on :3001; the client
# (RheaFileToolStep / rhea_adapter) resolves its endpoint from this URL.
_RHEA_MCP_URL_DEFAULT = "http://localhost:3001/mcp/"


def autodiscover_rhea_env(*, dry_run: bool = False) -> dict[str, str]:
    """Probe the filesystem + apply platform defaults to populate any
    unset ``RHEA_*`` env vars the InfraOrchestrator needs.

    Returns a dict of {env_var_name: value} for EVERY var the
    function set (auto-discovered OR defaulted). Env vars the
    operator had already set are NOT in the returned dict — they
    were untouched.

    When ``dry_run`` is True the env is not mutated (callers can
    inspect what WOULD be set without applying it; useful for tests
    + ``apecx-setup verify``).
    """
    set_now: dict[str, str] = {}

    # 1. RHEA_REPO_PATH — try filesystem probes only when unset.
    if not _is_set(_RHEA_REPO_PATH):
        repo = _find_rhea_repo()
        if repo is not None:
            set_now[_RHEA_REPO_PATH] = str(repo)
            log.info(
                "Rhea autodiscovery: %s=%s (found via filesystem probe)",
                _RHEA_REPO_PATH,
                repo,
            )

    # 2. RHEA_PYTHON_PATH — derive from REPO_PATH (this run's OR a
    # previously-set one).
    if not _is_set(_RHEA_PYTHON_PATH):
        repo_str = set_now.get(_RHEA_REPO_PATH) or os.environ.get(_RHEA_REPO_PATH)
        if repo_str:
            venv_bin = Path(repo_str) / ".venv" / "bin"
            if (venv_bin / "python").is_file():
                set_now[_RHEA_PYTHON_PATH] = str(venv_bin)
                log.info(
                    "Rhea autodiscovery: %s=%s (derived from REPO_PATH)",
                    _RHEA_PYTHON_PATH,
                    venv_bin,
                )
            else:
                log.debug(
                    "Rhea autodiscovery: %s/python missing — this host-venv path is "
                    "legacy; the rhea-server now runs as an auto-built container, so a "
                    "host venv is not required",
                    venv_bin,
                )

    # 3. RHEA_MCP_URL — point the client at the container's :3001 endpoint
    # when the operator hasn't set it explicitly.
    if not _is_set(_RHEA_MCP_URL):
        set_now[_RHEA_MCP_URL] = _RHEA_MCP_URL_DEFAULT
        log.info(
            "Rhea autodiscovery: %s=%s (default — client points at the container)",
            _RHEA_MCP_URL,
            _RHEA_MCP_URL_DEFAULT,
        )

    # 4. macOS-specific defaults — only the platform that needs them.
    if platform.system() == "Darwin":
        if not _is_set(_RHEA_CONDA_ENVS_DIR):
            # $TMPDIR/apecx-rhea/conda/envs — writable, persists for the
            # session, doesn't leak into /tmp where tmpfs eviction could
            # surprise the operator mid-run.
            tmp_root = tempfile.gettempdir()  # honors $TMPDIR on macOS
            envs_dir = Path(tmp_root) / "apecx-rhea" / "conda" / "envs"
            if not dry_run:
                envs_dir.mkdir(parents=True, exist_ok=True)
            set_now[_RHEA_CONDA_ENVS_DIR] = str(envs_dir)
            log.info(
                "Rhea autodiscovery: %s=%s (macOS default — /home/rhea is autofs-read-only)",
                _RHEA_CONDA_ENVS_DIR,
                envs_dir,
            )

        if not _is_set(_PARSL_CONTAINER_BACKEND):
            # On macOS the docker-backend's --network=host is a no-op
            # so the Parsl worker can't reach the interchange. The
            # ``local`` backend uses a plain subprocess in the
            # rhea-server's network namespace. See rhea/manager/
            # parsl_config.py for the full rationale.
            set_now[_PARSL_CONTAINER_BACKEND] = "local"
            log.info(
                "Rhea autodiscovery: %s=local (macOS default — docker "
                "backend has no working --network=host)",
                _PARSL_CONTAINER_BACKEND,
            )

    # Apply.
    if not dry_run:
        for name, value in set_now.items():
            os.environ[name] = value

    return set_now


# ---------------------------------------------------------------------------
# Filesystem probes
# ---------------------------------------------------------------------------


def _is_set(name: str) -> bool:
    """True iff the operator has set this env var to a non-empty value.

    Empty string counts as 'not set' so an operator wanting to disable
    autodiscovery for a single var sets it to a real path or explicitly
    disables autodiscovery wholesale via ``APECX_RHEA_AUTODISCOVER=0``."""
    return bool(os.environ.get(name, "").strip())


def _find_rhea_repo() -> Path | None:
    """Search known locations for a Rhea checkout.

    A "valid" rhea repo is a directory containing both
    ``pyproject.toml`` and ``rhea/server/mcp_server.py``. The second
    guard rejects unrelated directories named ``rhea`` (e.g. the
    Greek-letter math library, a documentation folder, etc.).
    """
    candidates: list[Path] = []

    # 1. Sibling of apecx-mcp-integration (the standard workspace
    # layout). Resolved relative to this module's filesystem location,
    # which lives at <apecx>/src/apecx_integration/infrastructure/...
    # so parents[3] is the apecx repo root + parents[4] is the
    # workspace root that holds both repos.
    here = Path(__file__).resolve()
    workspace_candidate = here.parents[4] / "rhea"
    candidates.append(workspace_candidate)

    # 2. Common developer locations.
    home = Path.home()
    for rel in (
        "src/rhea",
        "code/rhea",
        "Downloads/apecx-cowork/rhea",
        "projects/rhea",
        "dev/rhea",
    ):
        candidates.append(home / rel)

    # 3. Anything the operator pre-pointed at (re-probe for symmetry
    # with the rest of the logic — if REPO_PATH is set to a stale
    # value we'd rather find a different valid one than blindly trust
    # the env var; but we already early-returned when REPO_PATH is
    # set, so this branch is dead in practice).

    for candidate in candidates:
        if _is_rhea_repo(candidate):
            return candidate
    return None


def _is_rhea_repo(path: Path) -> bool:
    """Validate a candidate directory as a Rhea checkout."""
    if not path.is_dir():
        return False
    if not (path / "pyproject.toml").is_file():
        return False
    return (path / "rhea" / "server" / "mcp_server.py").is_file()


# ---------------------------------------------------------------------------
# Opt-out hook
# ---------------------------------------------------------------------------


def autodiscovery_enabled() -> bool:
    """``APECX_RHEA_AUTODISCOVER=0`` disables the discovery.

    Useful for CI / scripted runs that want to enforce an explicit
    env block. Default: enabled.
    """
    return os.environ.get("APECX_RHEA_AUTODISCOVER", "1") != "0"


__all__ = [
    "autodiscover_rhea_env",
    "autodiscovery_enabled",
]
