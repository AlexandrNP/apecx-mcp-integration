"""Persistent Globus configuration — ``~/.apecx/globus_config.json``.

Holds only what the *user* customizes:

* ``dest_endpoint_id`` — the user-specific destination endpoint (their Globus
  Connect Personal UUID). Prompted once by ``apecx-globus-setup`` and persisted
  so the user does not re-enter it every shell.
* ``extra_source_dirs`` — additional source directories the user registered via
  ``apecx-globus-setup --add-dir``. Each is fetched **recursively** (everything
  under it) at transfer time, alongside the built-in BV-BRC / VIOLIN defaults.

The default source directories (BV-BRC, VIOLIN) are NOT stored here — they are
fixed code constants in ``_globus_data_transfer.py`` and never change, so the
no-arg setup applies them silently. This file carries customization only.

Path is ``$APECX_GLOBUS_CONFIG_PATH`` if set (used by tests), else
``~/.apecx/globus_config.json``. Unknown keys FAIL-LOUD (a typo must not be
silently dropped — workspace pydantic-extra-forbid discipline applied to JSON).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_DEFAULT_PATH = "~/.apecx/globus_config.json"
_ALLOWED_TOP_KEYS = {"dest_endpoint_id", "extra_source_dirs"}
_ALLOWED_DIR_KEYS = {"remote_path", "dest_subdir"}


def config_path() -> Path:
    """Resolve the config file path (``$APECX_GLOBUS_CONFIG_PATH`` override)."""
    raw = os.environ.get("APECX_GLOBUS_CONFIG_PATH", "").strip() or _DEFAULT_PATH
    return Path(raw).expanduser()


def _empty() -> dict[str, Any]:
    return {"dest_endpoint_id": None, "extra_source_dirs": []}


def _validate_dir_entry(entry: Any, *, where: str) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ValueError(f"globus_config: {where} must be an object, got {type(entry).__name__}")
    unknown = set(entry) - _ALLOWED_DIR_KEYS
    if unknown:
        raise ValueError(
            f"globus_config: {where} has unknown key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_DIR_KEYS)}"
        )
    remote = entry.get("remote_path")
    if not isinstance(remote, str) or not remote.strip():
        raise ValueError(f"globus_config: {where} needs a non-empty 'remote_path' string")
    sub = entry.get("dest_subdir")
    if sub is not None and (not isinstance(sub, str) or not sub.strip()):
        raise ValueError(
            f"globus_config: {where} 'dest_subdir' must be a non-empty string or omitted"
        )
    out = {"remote_path": remote.strip().rstrip("/")}
    out["dest_subdir"] = sub.strip().strip("/") if isinstance(sub, str) else _default_subdir(remote)
    return out


def _default_subdir(remote_path: str) -> str:
    """Derive a destination subdir from the remote path's last segment."""
    name = remote_path.strip().rstrip("/").rsplit("/", 1)[-1]
    return name or "extra"


def load() -> dict[str, Any]:
    """Load + validate the config. Returns defaults when the file is absent."""
    path = config_path()
    if not path.is_file():
        return _empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"globus_config: {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"globus_config: {path} must contain a JSON object, got {type(raw).__name__}"
        )
    unknown = set(raw) - _ALLOWED_TOP_KEYS
    if unknown:
        raise ValueError(
            f"globus_config: {path} has unknown key(s) {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_TOP_KEYS)}"
        )
    dest = raw.get("dest_endpoint_id")
    if dest is not None and (not isinstance(dest, str) or not dest.strip()):
        raise ValueError("globus_config: 'dest_endpoint_id' must be a non-empty string or null")
    dirs_raw = raw.get("extra_source_dirs", [])
    if not isinstance(dirs_raw, list):
        raise ValueError("globus_config: 'extra_source_dirs' must be a list")
    dirs = [_validate_dir_entry(e, where=f"extra_source_dirs[{i}]") for i, e in enumerate(dirs_raw)]
    return {
        "dest_endpoint_id": dest.strip() if isinstance(dest, str) else None,
        "extra_source_dirs": dirs,
    }


def save(cfg: dict[str, Any]) -> Path:
    """Validate + atomically write the config; returns the path written."""
    # Round-trip through load()'s validators by re-validating the pieces.
    dest = cfg.get("dest_endpoint_id")
    if dest is not None and (not isinstance(dest, str) or not dest.strip()):
        raise ValueError(
            "globus_config.save: 'dest_endpoint_id' must be a non-empty string or null"
        )
    dirs = [
        _validate_dir_entry(e, where=f"extra_source_dirs[{i}]")
        for i, e in enumerate(cfg.get("extra_source_dirs", []) or [])
    ]
    out = {
        "dest_endpoint_id": dest.strip() if isinstance(dest, str) else None,
        "extra_source_dirs": dirs,
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".globus_config_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def get_dest_endpoint() -> str | None:
    return load().get("dest_endpoint_id")


def set_dest_endpoint(endpoint_id: str) -> Path:
    if not isinstance(endpoint_id, str) or not endpoint_id.strip():
        raise ValueError("set_dest_endpoint: endpoint_id must be a non-empty string")
    cfg = load()
    cfg["dest_endpoint_id"] = endpoint_id.strip()
    return save(cfg)


def get_extra_source_dirs() -> list[dict[str, str]]:
    return load().get("extra_source_dirs", [])


def add_source_dir(remote_path: str, dest_subdir: str | None = None) -> dict[str, str]:
    """Append a recursive source directory. Idempotent on remote_path.

    Returns the stored entry (with the resolved dest_subdir).
    """
    entry = _validate_dir_entry(
        {"remote_path": remote_path, "dest_subdir": dest_subdir}
        if dest_subdir is not None
        else {"remote_path": remote_path},
        where="add_source_dir",
    )
    cfg = load()
    existing = cfg["extra_source_dirs"]
    for e in existing:
        if e["remote_path"] == entry["remote_path"]:
            # Update the dest_subdir in place rather than duplicating.
            e["dest_subdir"] = entry["dest_subdir"]
            save(cfg)
            return e
    existing.append(entry)
    save(cfg)
    return entry
