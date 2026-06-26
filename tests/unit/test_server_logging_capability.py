"""Unit test for the MCP ``logging`` capability registration (E3-S-followup).

The bug (E3-5): the FastMCP server did NOT advertise the MCP ``logging``
capability, so a standards-compliant client calling
``session.set_logging_level(...)`` during/after init got
``McpError: Method not found`` — which tears down the whole session before
the streaming tool runs. ``_register_logging_capability`` registers a no-op
``SetLevelRequest`` handler on the underlying low-level server, which both
makes ``logging/setLevel`` resolve (return ok) AND flips
``get_capabilities()`` to advertise ``logging``.

This is the fast (non-e2e) half of the proof; the real-client end-to-end
proof is ``tests/integration/test_mcp_stream_client.py``.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import EmptyResult, LoggingCapability, SetLevelRequest

from apecx_integration.mcp_surface.server import _configure_logging, _register_logging_capability


def test_logging_capability_advertised_after_registration():
    server = FastMCP("apecx-mcp-test")

    assert server._mcp_server.get_capabilities(NotificationOptions(), {}).logging is None

    _register_logging_capability(server)

    capabilities = server._mcp_server.get_capabilities(NotificationOptions(), {})
    assert isinstance(capabilities.logging, LoggingCapability)
    assert SetLevelRequest in server._mcp_server.request_handlers


@pytest.mark.asyncio
async def test_set_level_handler_returns_ok_for_any_level():
    """The handler accepts every level as a no-op and returns an EmptyResult —
    so ``logging/setLevel`` succeeds instead of erroring, without gating the
    stage-streaming channel on the chosen level."""
    server = FastMCP("apecx-mcp-test")
    _register_logging_capability(server)

    handler = server._mcp_server.request_handlers[SetLevelRequest]
    request = SetLevelRequest(params={"level": "info"})
    result = await handler(request)

    # ServerResult(EmptyResult()) — an "ok" with no payload, i.e. not an error.
    assert isinstance(result.root, EmptyResult)


def test_configure_logging_quiets_nanobrain_tree_by_default(monkeypatch):
    """#6 (startup default, distinct from the runtime setLevel above): nanobrain emits per-step
    INFO+DEBUG that floods a long-lived server's log (~95 MB/min under load). _configure_logging
    quiets the ``nanobrain`` tree to WARNING by default — a child INFO record is suppressed — and
    is env-overridable (``APECX_NANOBRAIN_LOG_LEVEL``)."""
    import logging

    nb = logging.getLogger("nanobrain")
    prior = nb.level
    try:
        monkeypatch.delenv("APECX_NANOBRAIN_LOG_LEVEL", raising=False)
        _configure_logging()
        assert nb.level == logging.WARNING
        assert not logging.getLogger("nanobrain.core.workflow").isEnabledFor(logging.INFO)
        # env override re-opens it for debugging
        monkeypatch.setenv("APECX_NANOBRAIN_LOG_LEVEL", "DEBUG")
        _configure_logging()
        assert nb.level == logging.DEBUG
    finally:
        nb.setLevel(prior)
