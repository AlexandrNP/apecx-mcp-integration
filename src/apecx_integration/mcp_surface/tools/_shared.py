"""Shared plumbing for the MCP tool modules.

Each tool module gets a lazily-initialized ``ControlPlaneClient``
built from ``APECX_CONTROL_PLANE_URL`` (default
``http://localhost:8000``). The server's ``main`` entry point wires
the tools in; tests inject a client via ``set_client`` to bypass
env-var lookup.

Kept tiny on purpose — the tools are one-line delegations.
"""

from __future__ import annotations

import os

from apecx_integration.mcp_surface.control_plane_client import (
    ControlPlaneClient,
)

_client: ControlPlaneClient | None = None


def set_client(client: ControlPlaneClient | None) -> None:
    """Inject a client (or clear it). Used by tests + the server's
    main() after it builds the production client."""
    global _client
    _client = client


def get_client() -> ControlPlaneClient:
    """Return the injected client, or build one lazily from env."""
    global _client
    if _client is None:
        base_url = os.environ.get(
            "APECX_CONTROL_PLANE_URL", "http://localhost:8000"
        )
        _client = ControlPlaneClient(base_url)
    return _client


__all__ = ["get_client", "set_client"]
