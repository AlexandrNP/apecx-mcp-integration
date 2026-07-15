"""Desktop re-ingestion adapter: the MCP tool boundary returns CONTENT (instructions + full report
with figures embedded INLINE as data-URIs + structured data) so a WEAK host LLM (Haiku) re-renders
everything, instead of a markdown-only dict it crops. Internal callers keep the dict (covered by
test_eo_primitives).

Figures ride inline as ``![...](data:image/png;base64,...)`` because Claude Desktop cannot read the
server-local artifacts dir — a bare ``figures/x.png`` path-link renders as a blank square.

Real-dependency parity: scripts/validate_fresh_install.py --full --e2e exercises a real desktop run
end-to-end; this pins the adapter shape + gating deterministically.
"""

from __future__ import annotations

import base64
import io

import pytest
from mcp.server.fastmcp import Image
from PIL import Image as PILImage

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.mcp_surface.tools import eo_primitives


class _Ctx:
    """A minimal client-present stand-in (any non-None object marks the desktop client path)."""


def _real_png(path) -> str:
    """Write a real (Pillow-openable) tiny PNG so the data-URI path actually produces output."""
    PILImage.new("RGB", (24, 24), (200, 0, 0)).save(str(path), format="PNG")
    return str(path)


def _result(fig_path: str) -> dict:
    return {
        "status": "ok",
        # the report REFERENCES the figure by basename — the adapter rewrites this to a data-URI
        "markdown": (
            "# Answer\nThe epitope map.\n\n"
            "## Structural evidence\n![surface render](figures/2XFB.png)\nSee the surface render."
        ),
        "run_id": "r1",
        "data_handle": "hdl:abc123",
        "data_preview": {"n_exposed": 7},
        "provenance": {"pymol_version": "3.1.0"},
        "artifacts_dir": "/Users/x/.apecx/artifacts/r1",
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


def test_desktop_payload_embeds_data_uri_and_structured(tmp_path):
    png = _real_png(tmp_path / "2XFB.png")
    items = eo_primitives._desktop_host_payload(_result(png))

    # exactly two items: [text-with-inline-images, structured-json] — NO separate Image blocks
    assert len(items) == 2
    assert not any(isinstance(it, Image) for it in items), (
        "figures are inline data-URIs, not Image blocks"
    )

    # [0] = explicit host instructions + the FULL markdown report (not cropped)
    assert isinstance(items[0], str)
    assert "INSTRUCTIONS FOR THE ASSISTANT" in items[0]
    assert "## Structural evidence" in items[0]  # a back-half section survives in full

    # the figure ref was rewritten to an inline data-URI; the unreachable path-link is GONE
    assert "data:image/png;base64," in items[0]
    assert "](figures/2XFB.png)" not in items[0], "the blank-square path-link must be replaced"

    # the artifacts dir is surfaced so a filesystem-capable user can open the originals
    assert "/Users/x/.apecx/artifacts/r1" in items[0]

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
    png = _real_png(tmp_path / "f.png")
    res = _result(png)
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


def test_data_uri_survives_fastmcp_content_conversion(tmp_path):
    """The inline data-URI must survive FastMCP's OWN pipeline (_convert_to_content) as TextContent —
    that is what carries the base64 image into Claude Desktop's rendered narrative. Committed so the
    inline-image path can't silently regress back to unreachable path-links."""
    from mcp.server.fastmcp.utilities.func_metadata import _convert_to_content
    from mcp.types import TextContent

    png = _real_png(tmp_path / "fig.png")
    blocks = _convert_to_content(eo_primitives._desktop_host_payload(_result(png)))
    text_blocks = [b for b in blocks if isinstance(b, TextContent)]
    assert any("data:image/png;base64," in b.text for b in text_blocks), (
        "data-URI lost in conversion"
    )
    assert any("INSTRUCTIONS FOR THE ASSISTANT" in b.text for b in text_blocks)


def test_adapter_degrades_loud_on_unreadable_figure():
    # a figure path that does not exist can't be encoded → the broken link is DROPPED (no blank square),
    # never a crash — payload still returns the text + structured blocks
    # basename matches the markdown ref (2XFB.png) so it IS recognized as a figure, but the file is
    # unreadable → _figure_data_uri returns None → the link is dropped
    items = eo_primitives._desktop_host_payload(_result("/nonexistent/2XFB.png"))
    assert isinstance(items[0], str) and "INSTRUCTIONS FOR THE ASSISTANT" in items[0]
    # inspect the REPORT BODY (after the instructions separator) — the instructions themselves carry
    # a `data:image/png;base64,...` example, so we must not match on the whole blob
    report_body = items[0].split("\n\n---\n\n", 1)[-1]
    assert "data:image/png;base64," not in report_body  # nothing encodable
    assert (
        "](figures/2XFB.png)" not in report_body
    )  # the broken link was neutralized, not left dangling
    assert any(isinstance(it, str) and it.startswith("STRUCTURED DATA") for it in items)


def test_figure_data_uri_encodes_and_downscales(tmp_path):
    """_figure_data_uri returns a data:image/png;base64 string that decodes to a valid, SMALLER PNG."""
    src = tmp_path / "big.png"
    PILImage.new("RGB", (900, 700), (128, 128, 128)).save(str(src), format="PNG")
    uri = eo_primitives._figure_data_uri(str(src))
    assert uri and uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    with PILImage.open(io.BytesIO(raw)) as im:
        assert im.format == "PNG"
        assert max(im.size) <= 400, "figure must be downscaled to bound the relayed payload"


def test_figure_data_uri_none_on_bad_path():
    assert eo_primitives._figure_data_uri("/nonexistent/nope.png") is None


def test_figure_data_uri_none_when_pillow_absent(monkeypatch, tmp_path):
    """Clean-install belt-and-suspenders: if Pillow is somehow absent the helper degrades to None
    (the caller drops the link) instead of crashing the tool."""
    png = _real_png(tmp_path / "f.png")
    import builtins

    real_import = builtins.__import__

    def _no_pil(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("Pillow not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_pil)
    assert eo_primitives._figure_data_uri(png) is None
