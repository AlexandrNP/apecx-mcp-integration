"""Desktop re-ingestion adapter: the MCP tool boundary returns CONTENT (instructions + full report +
figure Image blocks + structured data) so a WEAK host LLM (Haiku) re-renders everything, instead of a
markdown-only dict it crops. Internal callers keep the dict (covered by test_eo_primitives).

Real-dependency parity: scripts/validate_fresh_install.py --full --e2e exercises a real desktop run
end-to-end; this pins the adapter shape + gating deterministically.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import Image

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.mcp_surface.tools import eo_primitives


class _Ctx:
    """A minimal client-present stand-in (any non-None object marks the desktop client path)."""


def _result(fig_path: str) -> dict:
    return {
        "status": "ok",
        "markdown": "# Answer\nThe epitope map.\n\n## Structural evidence\nSee the surface render.",
        "run_id": "r1",
        "data_handle": "hdl:abc123",
        "data_preview": {"n_exposed": 7},
        "provenance": {"pymol_version": "3.1.0"},
        "artifacts": [
            {"name": "figures/2XFB.png", "path": fig_path, "kind": "figure"},
            {
                "name": "tool_outputs/structural_sasa.json",
                "path": "/x",
                "kind": "tool_output",
                # a LARGE embedded blob the adapter must NOT dump wholesale (would blow Haiku context)
                "text": '{"exposed_residues": [' + ", ".join(str(i) for i in range(2000)) + "]}",
            },
        ],
    }


@pytest.fixture
def _restore_locus():
    orig = get_active_locus()
    yield
    set_active_locus(orig)


def test_desktop_payload_has_instructions_image_and_structured(tmp_path):
    png = tmp_path / "2XFB.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    items = eo_primitives._desktop_host_payload(_result(str(png)))

    # [0] = explicit host instructions + the FULL markdown report (not cropped)
    assert isinstance(items[0], str)
    assert "INSTRUCTIONS FOR THE ASSISTANT" in items[0]
    assert "## Structural evidence" in items[0]  # a back-half section survives in full

    # the figure is attached as an MCP Image content block (base64 → Desktop renders inline)
    assert any(isinstance(it, Image) for it in items), "the figure must be an Image content block"

    # the structured-data block carries the manifest + precise values
    structured = [it for it in items if isinstance(it, str) and it.startswith("STRUCTURED DATA")]
    assert structured, "missing the structured-data block"
    assert '"n_exposed": 7' in structured[0]  # data_preview value
    assert "figures/2XFB.png" in structured[0]  # lean manifest entry
    assert "hdl:abc123" in structured[0]  # data_handle preserved (host can fetch the full payload)
    # the 64KB tool_output blob is STRIPPED — keep the weak host LLM's context safe (no `text` field)
    assert '"text"' not in structured[0]
    assert "exposed_residues" not in structured[0]
    assert len(structured[0]) < 4096, "structured block must stay lean for a weak host LLM"


def test_maybe_desktop_payload_gating(tmp_path, _restore_locus):
    png = tmp_path / "f.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nx")
    res = _result(str(png))
    ctx = _Ctx()

    # headless (no ctx) → unchanged dict
    assert eo_primitives.maybe_desktop_payload(res, None) is res
    # non-ok (error / needs_input keep control_transfer + error fields) → unchanged dict
    err = {**res, "status": "error"}
    assert eo_primitives.maybe_desktop_payload(err, ctx) is err

    # AGENT locus → unchanged dict (the apecx LLM synthesizes; no host re-ingestion)
    set_active_locus(ExecutionLocus.AGENT)
    assert eo_primitives.maybe_desktop_payload(res, ctx) is res

    # DESKTOP locus + ctx + ok → the content-list payload
    set_active_locus(ExecutionLocus.DESKTOP)
    out = eo_primitives.maybe_desktop_payload(res, ctx)
    assert isinstance(out, list)
    assert "INSTRUCTIONS FOR THE ASSISTANT" in out[0]
    # "partial" is a COMPLETED degrade-loud run (full report + figures) → also gets the payload,
    # so the images aren't lost just because one optional leg degraded.
    assert isinstance(eo_primitives.maybe_desktop_payload({**res, "status": "partial"}, ctx), list)


def test_figure_image_converts_to_real_base64_mcp_imagecontent(tmp_path):
    """The figure Image must convert to a real base64 MCP ImageContent through FastMCP's OWN pipeline
    (_convert_to_content) — that is the mechanism that makes Claude Desktop render it inline rather
    than show an unusable path. Committed so the base64 path can't silently regress."""
    from mcp.server.fastmcp.utilities.func_metadata import _convert_to_content
    from mcp.types import ImageContent, TextContent

    png = tmp_path / "fig.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")
    blocks = _convert_to_content(eo_primitives._desktop_host_payload(_result(str(png))))
    img = [b for b in blocks if isinstance(b, ImageContent)]
    assert img and img[0].data, "no base64 ImageContent produced from the figure"
    assert img[0].mimeType == "image/png"
    assert any(
        isinstance(b, TextContent) and "INSTRUCTIONS FOR THE ASSISTANT" in b.text for b in blocks
    )


def test_adapter_degrades_loud_on_unreadable_figure():
    # a figure path that does not exist must be SKIPPED, not crash — payload still returns text blocks
    items = eo_primitives._desktop_host_payload(_result("/nonexistent/x.png"))
    assert isinstance(items[0], str) and "INSTRUCTIONS FOR THE ASSISTANT" in items[0]
    assert not any(isinstance(it, Image) for it in items)  # the bad figure was skipped
    assert any(isinstance(it, str) and it.startswith("STRUCTURED DATA") for it in items)
