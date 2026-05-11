"""Workspace-root resolver — apecx-mcp-integration's thin wrapper around
``nanobrain.library.runtime.workspace_root.locate_workflow_root`` (G40).

A single resolver replaces an earlier-spread-out pattern of
``Path(__file__).resolve().parents[N]`` calls scattered across multiple
modules — that pattern broke silently in non-standard checkout layouts
(monorepos, vendored, container builds) where parent depth differs.

## G40-WA-1 closure (2026-05-11)

This module used to carry its own marker-walk implementation,
duplicating logic the framework now ships at
``nanobrain.library.runtime.workspace_root.locate_workflow_root``.
The duplicate is retired; this module now delegates.

The function's caller-facing signature is preserved for backward
compatibility:

  * ``starting_file`` — caller's ``__file__``
  * ``fallback_depth`` — ``parents[N]`` depth used as a last-resort
    when no markers are found and the env-var override is unset

Resolution order (first hit wins):

  1. ``APECX_WORKSPACE_ROOT`` env var — explicit operator override.
     Always honored when set, even if the path doesn't exist.
  2. Walk upward from the calling site looking for a directory that
     contains ``apecx-mcp-integration/`` as a subdirectory. (Framework
     helper handles the walk; we just pass our preferred marker.)
  3. Fallback: ``parents[fallback_depth]`` for the documented standard
     checkout layout.
"""

from __future__ import annotations

from pathlib import Path

from nanobrain.library.runtime.workspace_root import locate_workflow_root


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
    here = Path(starting_file).resolve()
    # Pass the file's parent (or the path itself if it's already a
    # directory) so the framework helper walks from a valid starting
    # directory. ``locate_workflow_root`` handles non-existent starts
    # gracefully but a file-path start is wasted work.
    start_dir = here.parent if here.is_file() else here

    found = locate_workflow_root(
        start=start_dir,
        markers=["apecx-mcp-integration"],
        env_var="APECX_WORKSPACE_ROOT",
    )
    if found is not None:
        return found

    # Fallback: documented standard checkout depth.
    return here.parents[fallback_depth]


__all__ = ["resolve_workspace_root"]
