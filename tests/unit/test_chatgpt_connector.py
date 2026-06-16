"""ChatGPT client support: the HTTP transport flag + the apecx-setup connector guidance.

ChatGPT's MCP support is a REMOTE HTTP connector added through the ChatGPT UI (no local config
file to write, unlike Claude Desktop), reachable only at a PUBLIC HTTPS URL. So apecx-mcp must
(a) be able to SERVE HTTP (it is stdio-only otherwise → ChatGPT cannot connect at all), and
(b) apecx-setup must print the serve → tunnel → add-connector steps. These tests pin both.
"""

from __future__ import annotations

from apecx_integration.cli.setup import _step_chatgpt, chatgpt_connector_guidance
from apecx_integration.mcp_surface.server import _build_arg_parser


def test_apecx_mcp_supports_streamable_http_transport_for_chatgpt():
    parser = _build_arg_parser()
    # MINIMUM arguments: only --transport. Host/port are FastMCP defaults (no extra flags).
    ns = parser.parse_args(["--transport", "streamable-http"])
    assert ns.transport == "streamable-http"
    assert not hasattr(ns, "port") and not hasattr(ns, "host")  # no port/host bloat
    # default stays stdio (Claude Desktop / local clients)
    assert parser.parse_args([]).transport == "stdio"


def test_chatgpt_guidance_is_minimal_and_uses_the_default_port():
    g = chatgpt_connector_guidance()
    # ONE-arg serve command, the DEFAULT port (not a random example), the /mcp endpoint
    assert "apecx-mcp --transport streamable-http" in g
    assert "--port" not in g  # no extra argument in the instruction
    assert ":8000/mcp" in g  # the actual default port apecx-mcp HTTP serves on
    # a tunnel (noted as a separate install) + the UI add steps
    assert ("ngrok" in g or "cloudflared" in g) and "separately" in g.lower()
    assert "Apps & Connectors" in g and "Connector URL" in g


def test_step_chatgpt_runs_and_reports_ok(capsys):
    result = _step_chatgpt(interactive=False)
    assert result.name == "chatgpt"
    assert result.status == "ok"
    out = capsys.readouterr().out
    assert "ChatGPT" in out and "--transport streamable-http" in out
