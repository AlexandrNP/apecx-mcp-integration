"""Workspace-root resolver — shared by code that needs to find the
apecx-cowork workspace root (where the sibling repos and the shared
``data/`` directory live).

A single resolver replaces an earlier-spread-out pattern of
``Path(__file__).resolve().parents[N]`` calls scattered across multiple
modules — that pattern broke silently in non-standard checkout layouts
(monorepos, vendored, container builds) where parent depth differs.

Resolution order (first hit wins):

1. ``APECX_WORKSPACE_ROOT`` env var — explicit operator override.
   Always honored when set, even if the path doesn't exist (the caller
   will produce a clearer error than silent fallback).
2. Walk upward from the calling site looking for a directory that
   contains ``apecx-mcp-integration/`` plus at least one of
   ``nanobrain/``, ``_workspace_notes/``, or ``data/`` as siblings.
   This is the canonical workspace shape.
3. Fallback: ``parents[N]`` for the documented standard checkout.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_workspace_root(starting_file: str | Path, fallback_depth: int) -> Path:
    """Locate the apecx-cowork workspace root.

    Args:
        starting_file: Caller's ``__file__`` — the resolver walks up
            from this path looking for workspace markers.
        fallback_depth: ``parents[N]`` depth to use when no marker
            directories are found and ``APECX_WORKSPACE_ROOT`` is
            unset. Pass the depth that's correct for the caller's
            position in a standard workspace checkout.

    Returns:
        The resolved workspace root path. Note: the path is NOT
        guaranteed to exist — callers should validate before relying
        on contained data.
    """
    explicit = os.environ.get("APECX_WORKSPACE_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    here = Path(starting_file).resolve()
    for ancestor in here.parents:
        if (ancestor / "apecx-mcp-integration").is_dir() and (
            (ancestor / "nanobrain").is_dir()
            or (ancestor / "_workspace_notes").is_dir()
            or (ancestor / "data").is_dir()
        ):
            return ancestor

    return here.parents[fallback_depth]


__all__ = ["resolve_workspace_root"]
