"""Default data-directory constants for the VIOLIN + BV-BRC tabular
fixtures.

The ``VIOLINBVBRCContextStep`` that previously lived here was retired
2026-06-15 alongside the ``violin_bvbrc`` workflow. The two default
path constants below survive because the synthesis-assembly steps
(``SynthesisContextAssemblyStep`` /
``UnlimitedSynthesisAssemblyStep``) import them to resolve VIOLIN /
BV-BRC data directories. The stateless lookup logic those steps use
lives in ``_violin_bvbrc_lookup`` and ``_unlimited_lookup``.
"""

from __future__ import annotations

from nanobrain.library.runtime.workspace_root import locate_workflow_root

# Workspace root via nanobrain's G40 helper. ``fallback_depth=5`` is
# the depth from this file (composition/steps/X.py) to the workspace
# root in a standard checkout — kept as a last-resort when no marker
# is found and the env var is unset. The shim that used to wrap this
# was retired 2026-05-16 (commit retires apecx_integration._workspace).
_WORKSPACE_ROOT = locate_workflow_root(
    start=__file__,
    markers=["apecx-mcp-integration"],
    env_var="APECX_WORKSPACE_ROOT",
    fallback_depth=5,
)
assert _WORKSPACE_ROOT is not None, (
    "locate_workflow_root returned None despite fallback_depth=5 — "
    "this file's parents chain is shorter than 5 levels (broken install?)"
)
_DEFAULT_VIOLIN_DIR = _WORKSPACE_ROOT / "data" / "violin"
_DEFAULT_BVBRC_DIR = _WORKSPACE_ROOT / "data" / "bvbrc_cache"
