"""Shared plumbing for the MCP tool modules.

Each tool module gets a lazily-initialized ``ControlPlaneClient`` built from the centralized
network config's ``control_plane_url`` (default ``http://127.0.0.1:8000``; see
``mcp_surface.network_config``). The server's ``main`` entry point wires the tools in; tests
inject a client via ``set_client`` to bypass the config lookup.

Kept tiny on purpose — the tools are one-line delegations.
"""

from __future__ import annotations

from uuid import UUID

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
    """Return the injected client, or build one lazily from the centralized network config."""
    global _client
    if _client is None:
        from apecx_integration.mcp_surface.network_config import get_network_config

        _client = ControlPlaneClient(get_network_config().control_plane_url)
    return _client


class InvalidRunIdError(ValueError):
    """Raised when a tool argument that should be a UUID is not.

    Carries the offending input verbatim so the MCP error message
    can echo it back to the caller (Claude, in practice) for self-
    correction.
    """

    def __init__(self, raw: str, field: str = "run_id"):
        super().__init__(
            f"{field}={raw!r} is not a valid UUID; expected a "
            "canonical UUID like '550e8400-e29b-41d4-a716-446655440000'."
        )
        self.raw = raw
        self.field = field


def parse_run_id(raw: str, *, field: str = "run_id") -> UUID:
    """Parse a UUID from a string with a friendly error.

    The Tier-1 MCP tools accept user-typed run-id strings (typically
    pasted by Claude after a `/workflows/start` response). A bare
    `UUID(raw)` raises a generic ``ValueError`` whose message ("badly
    formed hexadecimal UUID string") is correct but unhelpful — the
    caller can't tell which field failed or what was passed.
    Audit §3.1 (docs/codebase_audit_2026_04_24.md).
    """
    if not isinstance(raw, str):
        raise InvalidRunIdError(repr(raw), field=field)
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidRunIdError(raw, field=field) from exc


__all__ = ["InvalidRunIdError", "get_client", "parse_run_id", "set_client"]
