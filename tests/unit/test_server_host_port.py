"""Unit tests for apecx-mcp HTTP-transport host/port resolution.

The MCP server's HTTP bind address (for streamable-http / sse) is resolved by
``_resolve_http_host_port`` with precedence: CLI flag > env var > default. The port
defaults to 8001 (apecx-owned, distinct from the control plane on 8000); the host is
left None (FastMCP's 127.0.0.1) when unset. apecx owns this rather than deferring to
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

from apecx_integration.mcp_surface.server import (
    _assert_mcp_port_distinct_from_control_plane,
    _build_arg_parser,
    _resolve_http_host_port,
)


def _ns(host=None, port=None) -> argparse.Namespace:
    return argparse.Namespace(host=host, port=port)


def test_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("APECX_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("APECX_MCP_PORT", "9999")
    host, port = _resolve_http_host_port(_ns(host="0.0.0.0", port=8001))
    assert host == "0.0.0.0"
    assert port == 8001


def test_env_used_when_no_flag(monkeypatch):
    monkeypatch.setenv("APECX_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("APECX_MCP_PORT", "8001")
    host, port = _resolve_http_host_port(_ns())
    assert host == "0.0.0.0"
    assert port == 8001


def test_none_when_neither(monkeypatch):
    """With no flag/env, host stays None but port defaults to the apecx MCP HTTP port 8001."""
    monkeypatch.delenv("APECX_MCP_HOST", raising=False)
    monkeypatch.delenv("APECX_MCP_PORT", raising=False)
    host, port = _resolve_http_host_port(_ns())
    assert host is None
    assert port == 8001


def test_partial_host_only(monkeypatch):
    """A host without a port still gets the apecx MCP HTTP default port 8001."""
    monkeypatch.delenv("APECX_MCP_PORT", raising=False)
    host, port = _resolve_http_host_port(_ns(host="0.0.0.0"))
    assert host == "0.0.0.0"
    assert port == 8001


def test_bad_env_port_fails_loud(monkeypatch):
    monkeypatch.setenv("APECX_MCP_PORT", "not-a-number")
    with pytest.raises(ValueError, match="APECX_MCP_PORT must be an integer"):
        _resolve_http_host_port(_ns())


def test_out_of_range_port_fails_loud(monkeypatch):
    monkeypatch.delenv("APECX_MCP_PORT", raising=False)
    with pytest.raises(ValueError, match="1..65535"):
        _resolve_http_host_port(_ns(port=70000))


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


def test_guard_default_mcp_port_vs_default_cp_no_raise(monkeypatch):
    """The new default (8001) does NOT collide with the control plane's default 8000."""
    monkeypatch.delenv("APECX_CONTROL_PLANE_URL", raising=False)
    _assert_mcp_port_distinct_from_control_plane(None, 8001)


def test_guard_explicit_8000_vs_default_cp_raises(monkeypatch):
    """Binding MCP HTTP on the CP's loopback port 8000 fails loud before the boot."""
    monkeypatch.delenv("APECX_CONTROL_PLANE_URL", raising=False)
    with pytest.raises(ValueError, match="collides with the control plane"):
        _assert_mcp_port_distinct_from_control_plane(None, 8000)


def test_guard_remote_cp_same_port_no_raise(monkeypatch):
    """A REMOTE control plane (non-loopback host) on :8000 does not contend for the local socket."""
    monkeypatch.setenv("APECX_CONTROL_PLANE_URL", "http://prod-cp.internal:8000")
    _assert_mcp_port_distinct_from_control_plane(None, 8000)


def test_guard_specific_mcp_interface_same_port_no_raise(monkeypatch):
    """An MCP bound to a specific non-loopback interface does not collide with the loopback CP."""
    monkeypatch.delenv("APECX_CONTROL_PLANE_URL", raising=False)
    _assert_mcp_port_distinct_from_control_plane("10.0.0.5", 8000)
