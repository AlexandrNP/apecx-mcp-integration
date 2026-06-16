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
    ns = parser.parse_args(
        ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8811"]
    )
    assert ns.transport == "streamable-http"
    assert ns.host == "0.0.0.0"
    assert ns.port == 8811
    # default stays stdio (Claude Desktop / local clients)
    assert parser.parse_args([]).transport == "stdio"


def test_chatgpt_guidance_names_the_http_endpoint_tunnel_and_ui_steps():
    g = chatgpt_connector_guidance(port=8765)
    # the three load-bearing facts a ChatGPT user needs
    assert "--transport streamable-http" in g  # serve HTTP
    assert "/mcp" in g  # the endpoint path ChatGPT connects to
    assert "ngrok" in g and "https://" in g.lower()  # public HTTPS tunnel (localhost won't work)
    assert "Apps & Connectors" in g and "Connector URL" in g  # the UI add steps
    assert "no local config file" in g.lower()  # honest: nothing to auto-write


def test_step_chatgpt_runs_and_reports_ok(capsys):
    result = _step_chatgpt(interactive=False)
    assert result.name == "chatgpt"
    assert result.status == "ok"
    out = capsys.readouterr().out
    assert "ChatGPT" in out and "--transport streamable-http" in out
