"""Bootstrap entry point — drive the dictionary-build workflow once.

This module is the **migration seam** between the trigger model the
project ships today (a3 — "lazy at MCP startup") and the long-term
target (a1 — "harvester invokes after a harvest run completes"). Both
callers funnel through :func:`ensure_dictionary`, so migrating from a3
to a1 is purely a question of *where* the function is called from.

What it does
------------
1. Resolve a :class:`EnsureDictionaryConfig` (defaults derived from env
   vars; caller may override).
2. If the target SQLite already exists, return its path immediately —
   the build is idempotent and re-running it is a 10–15 minute waste.
3. If the operator opted out via ``APECX_SKIP_DICT_BUILD=1``, return
   ``None`` with a clear log line.
4. If required input files are absent, return ``None`` with a clear log
   line — the build CANNOT succeed without VIOLIN data.
5. Otherwise, load the dictionary-build workflow YAML, drive the
   cascade, and return the resulting SQLite path.

Why a single function and not a class
-------------------------------------
There is no per-call state worth a class. Every invocation is independent
and re-loads the workflow YAML. A future Phase-2 caching layer (the
harvester sink) would live in the harvester, not here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

WORKFLOW_YAML = Path(__file__).parent / "configs" / "dictionary_build_workflow.yml"

_DEFAULT_DATA_ROOT = Path("~/.apecx/data").expanduser()
_DEFAULT_DICT_OUTPUT_DIR = Path("~/.apecx/dictionary").expanduser()
_DEFAULT_TAXDUMP_DIR = Path("~/.apecx/taxdump").expanduser()


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


@dataclass
class EnsureDictionaryConfig:
    """Inputs to :func:`ensure_dictionary`.

    Defaults read from environment variables so callers can leave the
    instance empty for the common case. Every field is independently
    overridable.
    """

    sqlite_path: Path | None = None
    """Target dictionary file. Defaults to ``${APECX_SYNONYM_DICT_PATH}``
    if set, else ``${APECX_DICT_OUTPUT_DIR or ~/.apecx/dictionary}/dictionary.sqlite``."""

    data_root: Path | None = None
    """Root containing ``violin/*.csv`` and ``bvbrc/*.tsv``. Defaults to
    ``${APECX_DATA_ROOT}`` if set, else ``~/.apecx/data``."""

    taxdump_dir: Path | None = None
    """Where the taxdump fetch step writes ``nodes.dmp`` / ``merged.dmp``."""

    skip_if_data_missing: bool = True
    """If True (default), return None when VIOLIN data is absent. If False,
    let the workflow surface its own error."""

    cascade_timeout_seconds: float = 1800.0
    """Wall-clock budget for the cascade. Default 30 min covers a full
    OLS-driven build of all four tables; reduce for smoke tests."""

    extra_env: dict[str, str] = field(default_factory=dict)
    """Additional env vars to inject before driving the workflow. Use
    this to override step-config defaults without mutating ``os.environ``
    permanently — :func:`ensure_dictionary` restores prior values on
    return."""

    def resolve(self) -> EnsureDictionaryConfig:
        """Fill in env-var-derived defaults; return a fully-resolved copy."""
        sqlite = self.sqlite_path
        if sqlite is None:
            env_dict = os.environ.get("APECX_SYNONYM_DICT_PATH", "").strip()
            if env_dict:
                sqlite = Path(env_dict).expanduser()
            else:
                out_dir = _env_path("APECX_DICT_OUTPUT_DIR", _DEFAULT_DICT_OUTPUT_DIR)
                sqlite = out_dir / "dictionary.sqlite"

        return EnsureDictionaryConfig(
            sqlite_path=sqlite,
            data_root=self.data_root or _env_path("APECX_DATA_ROOT", _DEFAULT_DATA_ROOT),
            taxdump_dir=self.taxdump_dir or _env_path("APECX_TAXDUMP_DIR", _DEFAULT_TAXDUMP_DIR),
            skip_if_data_missing=self.skip_if_data_missing,
            cascade_timeout_seconds=self.cascade_timeout_seconds,
            extra_env=dict(self.extra_env),
        )


def _required_data_files(data_root: Path) -> list[Path]:
    """At least one of these must exist for the build to be meaningful."""
    return [
        data_root / "violin" / "Pathogen_Information.csv",
        data_root / "violin" / "Vaccine_Information.csv",
        data_root / "violin" / "Gene_Information.csv",
    ]


def _data_present(data_root: Path) -> bool:
    return any(p.exists() for p in _required_data_files(data_root))


def ensure_dictionary(config: EnsureDictionaryConfig | None = None) -> Path | None:
    """Synchronous wrapper around :func:`ensure_dictionary_async`.

    Returns the path to the dictionary SQLite file, or ``None`` if the
    build was skipped (artifact already present, opt-out, or missing
    inputs). Callers should treat ``None`` as "proceed without the
    fast-path; entity resolution will fall back to slow substring search."
    """
    return asyncio.run(ensure_dictionary_async(config))


async def ensure_dictionary_async(
    config: EnsureDictionaryConfig | None = None,
) -> Path | None:
    """Build the synonym dictionary if it isn't already present.

    Async because the underlying nanobrain workflow is async. Most
    callers will use the sync :func:`ensure_dictionary` wrapper; tests
    and async hosts (a future harvester sink) call this directly.
    """
    cfg = (config or EnsureDictionaryConfig()).resolve()
    sqlite = cfg.sqlite_path
    assert sqlite is not None  # resolve() guarantees this

    # Idempotency check — single most common case is "already built".
    if sqlite.is_file():
        log.info("synonym dictionary already present at %s — skipping build", sqlite)
        return sqlite

    if os.environ.get("APECX_SKIP_DICT_BUILD", "").strip() == "1":
        log.warning(
            "APECX_SKIP_DICT_BUILD=1 set — declining to build dictionary at %s",
            sqlite,
        )
        return None

    if cfg.skip_if_data_missing and not _data_present(cfg.data_root or _DEFAULT_DATA_ROOT):
        log.warning(
            "synonym dictionary build skipped — no VIOLIN data found under %s. "
            "Run 'apecx-setup' first to download VIOLIN/BV-BRC, then re-launch.",
            cfg.data_root,
        )
        return None

    log.warning(
        "Building synonym dictionary at %s — first run can take 10–15 minutes "
        "of live OLS calls. Set APECX_SKIP_DICT_BUILD=1 to opt out and use "
        "slow substring resolution instead.",
        sqlite,
    )

    return await _drive_workflow(cfg)


async def _drive_workflow(cfg: EnsureDictionaryConfig) -> Path:
    """Load the dictionary-build workflow YAML and drive the cascade."""
    # Imported here so module import doesn't pull in the full nanobrain
    # framework when callers only want the config dataclass.
    from nanobrain.core.workflow import Workflow

    saved_env: dict[str, str | None] = {}
    env_overrides = {
        "APECX_DATA_ROOT": str(cfg.data_root) if cfg.data_root else None,
        "APECX_TAXDUMP_DIR": str(cfg.taxdump_dir) if cfg.taxdump_dir else None,
        "APECX_DICT_OUTPUT_DIR": str(cfg.sqlite_path.parent) if cfg.sqlite_path else None,
        **cfg.extra_env,
    }

    # Workaround for a nanobrain bug (async_logging.py:103, logging_system.py:1051):
    # both default the log directory to ``Path("logs")`` — relative to cwd. When
    # apecx-mcp is launched from Claude Desktop on macOS, cwd is ``/`` (read-only),
    # so the logger crashes with ``[Errno 30] Read-only file system: 'logs'`` and
    # the workflow instantiation fails. We chdir to a writable apecx state dir for
    # the duration of the workflow so any cwd-relative ``logs/`` resolves under it.
    # Restored in finally so the parent process's cwd is unaffected.
    log_root = Path("~/.apecx").expanduser()
    log_root.mkdir(parents=True, exist_ok=True)
    prev_cwd = os.getcwd()

    try:
        os.chdir(log_root)

        for key, value in env_overrides.items():
            saved_env[key] = os.environ.get(key)
            if value is not None:
                os.environ[key] = value

        workflow = Workflow.from_config(str(WORKFLOW_YAML))
        await workflow.initialize()

        init_result = await workflow.process({"trigger": True})
        if init_result is None or init_result.get("status") != "data_flow_initiated":
            raise RuntimeError(
                f"workflow.process did not enter data-driven mode; got: {init_result!r}"
            )

        drained = await workflow.wait_for_cascade(
            timeout=cfg.cascade_timeout_seconds,
            settle_ms=200,
        )
        if not drained:
            raise TimeoutError(
                f"dictionary-build cascade did not drain within "
                f"{cfg.cascade_timeout_seconds}s — check logs for hung steps"
            )
    finally:
        # Restore env + cwd even on exception so a failed bootstrap doesn't
        # corrupt the parent process's environment.
        os.chdir(prev_cwd)
        for key, prior in saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior

    if not cfg.sqlite_path.is_file():
        raise RuntimeError(f"workflow drained but SQLite artifact not found at {cfg.sqlite_path}")

    log.info("synonym dictionary built at %s", cfg.sqlite_path)
    return cfg.sqlite_path
