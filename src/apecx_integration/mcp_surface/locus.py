"""execution_locus — re-export of the canonical locus module.

The implementation lives in ``composition/runtime/execution_locus`` so composition steps can
read the active locus without importing UP into ``mcp_surface`` (layer order
``mcp_surface → composition``). This module re-exports it under the ``mcp_surface.locus`` name
the server + reasoning surface use; importing DOWN into composition is allowed.
"""

from __future__ import annotations

from apecx_integration.composition.runtime.execution_locus import (
    ENV_VAR,
    ExecutionLocus,
    get_active_locus,
    resolve_locus,
    set_active_locus,
)

__all__ = [
    "ENV_VAR",
    "ExecutionLocus",
    "get_active_locus",
    "resolve_locus",
    "set_active_locus",
]
