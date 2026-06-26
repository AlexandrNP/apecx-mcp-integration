"""Unit tests for apecx-mcp HTTP-transport host/port resolution.

The MCP server's HTTP bind address (for streamable-http / sse) is resolved by
``_resolve_http_host_port`` with precedence: **CLI flag > config > built-in**. There is
NO env layer — the centralized YAML config (``network_config.NetworkConfig``) is the
single source; ``mcp.host`` / ``mcp.port`` carry the defaults (127.0.0.1 / 8001, the
port distinct from the control plane on 8000). apecx owns this rather than deferring to
FastMCP's $FASTMCP_HOST/$FASTMCP_PORT binding, which proved unreliable in a real
server deployment (the operator had to monkeypatch ``server.settings`` by hand).
``_assert_mcp_port_distinct_from_control_plane`` then rejects a local MCP/CP port
collision upfront (before the boot) instead of crashing late in uvicorn.

Real-data coverage: the end-to-end bind is verified by serving
``apecx-mcp --transport streamable-http --host 0.0.0.0 --port 8001`` and completing an
MCP initialize handshake over curl (recorded in the Phase-1 commit message); these unit
tests pin the resolution logic that drives it.
"""

from __future__ import annotations

import argparse

import pytest

from apecx_integration.mcp_surface.network_config import (
    ControlPlaneConfig,
    MCPConfig,
    NetworkConfig,
)
from apecx_integration.mcp_surface.server import (
    _assert_mcp_port_distinct_from_control_plane,
    _build_arg_parser,
    _resolve_http_host_port,
)


def _ns(host=None, port=None) -> argparse.Namespace:
    return argparse.Namespace(host=host, port=port)


# ---------------------------------------------------------------------------
# _resolve_http_host_port — precedence CLI flag > config > built-in
# ---------------------------------------------------------------------------


def test_flag_wins_over_config():
    """An explicit --port beats the config's mcp.port."""
    host, port = _resolve_http_host_port(_ns(port=9090), NetworkConfig())
    assert port == 9090
    # host falls back to the config default (no --host passed)
    assert host == "127.0.0.1"


def test_config_default_when_no_flag():
    """With no flag, the bind comes from the config defaults (127.0.0.1 / 8001)."""
    host, port = _resolve_http_host_port(_ns(), NetworkConfig())
    assert host == "127.0.0.1"
    assert port == 8001


def test_config_override_used_when_no_flag():
    """A config that sets mcp.host/mcp.port supplies the bind when no flag is passed."""
    cfg = NetworkConfig(mcp=MCPConfig(host="0.0.0.0", port=9001))
    host, port = _resolve_http_host_port(_ns(), cfg)
    assert host == "0.0.0.0"
    assert port == 9001


def test_flag_host_beats_config_host():
    """An explicit --host beats the config's mcp.host."""
    cfg = NetworkConfig(mcp=MCPConfig(host="127.0.0.1", port=8001))
    host, port = _resolve_http_host_port(_ns(host="0.0.0.0"), cfg)
    assert host == "0.0.0.0"
    assert port == 8001  # port falls back to config default


def test_out_of_range_cli_port_fails_loud():
    """An explicit --port outside 1..65535 is range-checked at the resolver and FAILS LOUD."""
    with pytest.raises(ValueError, match="1..65535"):
        _resolve_http_host_port(_ns(port=70000), NetworkConfig())


# ---------------------------------------------------------------------------
# _build_arg_parser
# ---------------------------------------------------------------------------


def test_arg_parser_accepts_host_port():
    parser = _build_arg_parser()
    args = parser.parse_args(
        ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8001"]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 8001
    assert args.transport == "streamable-http"


def test_arg_parser_defaults_host_port_none():
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.host is None
    assert args.port is None


# ---------------------------------------------------------------------------
# _assert_mcp_port_distinct_from_control_plane — reads config.control_plane
# ---------------------------------------------------------------------------


def test_guard_default_mcp_port_vs_default_cp_no_raise():
    """The default MCP port (8001) does NOT collide with the control plane's default 8000."""
    _assert_mcp_port_distinct_from_control_plane("127.0.0.1", 8001, NetworkConfig())


def test_guard_explicit_8000_vs_default_cp_raises():
    """Binding MCP HTTP on the CP's loopback port 8000 fails loud before the boot."""
    with pytest.raises(ValueError, match="collides with the control plane"):
        _assert_mcp_port_distinct_from_control_plane("127.0.0.1", 8000, NetworkConfig())


def test_guard_remote_cp_same_port_no_raise():
    """A REMOTE control plane (non-loopback host) on :8000 does not contend for the local socket."""
    cfg = NetworkConfig(control_plane=ControlPlaneConfig(host="prod-cp.internal", port=8000))
    _assert_mcp_port_distinct_from_control_plane("127.0.0.1", 8000, cfg)


def test_guard_specific_mcp_interface_same_port_no_raise():
    """An MCP bound to a specific non-loopback interface does not collide with the loopback CP."""
    _assert_mcp_port_distinct_from_control_plane("10.0.0.5", 8000, NetworkConfig())
