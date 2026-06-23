"""WS3b: persist an on-demand-synthesized rhea tool step as a portable, committed
wrapper YAML. Deterministic persist-logic tests use the REAL muscle spec shape;
a live-gated test does the full synthesize -> persist -> load against a real rhea.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import yaml
from nanobrain.library.tools.rhea_step_synthesizer import RheaStepSpec

from apecx_integration.composition.rhea_tool_persistence import persist_rhea_step

try:
    from nanobrain.library.tools.rhea_step_synthesizer import _UNPINNED_VERSION
except ImportError:  # pragma: no cover - defensive
    _UNPINNED_VERSION = "unknown"

_RHEA_URL = "http://localhost:3001/mcp/"
_RFTS = "nanobrain.library.steps.rhea_file_tool_step.RheaFileToolStep"

# The real shape synthesize_rhea_step produced for muscle against a live worker.
_MUSCLE_CONFIG = {
    "tool_name": "muscle",
    "find_tools_query": "muscle multiple sequence alignment",
    "file_input_arg": "input_seqs",
    "static_tool_args": {
        "cluster": "upgmb",
        "outputFormat": "fasta",
        "run": "16",
        "iterations": 16,
        "diags": False,
    },
    "output_file_args": [],
    "mcp_url": _RHEA_URL,
}


def _muscle_spec(descriptor_id: str = "rhea:muscle@3.8.1551+galaxy0") -> RheaStepSpec:
    return RheaStepSpec(
        step_class=_RFTS,
        step_config=dict(_MUSCLE_CONFIG),
        uses_file_input=True,
        descriptor_id=descriptor_id,
        utd={},
    )


def test_persist_writes_portable_pinned_wrapper(tmp_path):
    path = persist_rhea_step(_muscle_spec(), dest_dir=tmp_path)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "AUTO-GENERATED" in text
    assert "rhea:muscle@3.8.1551+galaxy0" in text  # version pin in provenance
    cfg = yaml.safe_load(text)
    # Portability: the env-specific localhost URL must NOT be baked in.
    assert cfg["mcp_url"] == "${RHEA_MCP_URL}"
    assert "localhost" not in text
    assert cfg["file_input_arg"] == "input_seqs"
    assert cfg["name"].endswith("_tool_step")


def test_persist_refuses_unpinned_spec(tmp_path):
    spec = _muscle_spec(descriptor_id=f"rhea:foo@{_UNPINNED_VERSION}")
    with pytest.raises(ValueError, match="UNPINNED"):
        persist_rhea_step(spec, dest_dir=tmp_path)


def _rhea_up() -> bool:
    try:
        return httpx.get(_RHEA_URL, timeout=3).status_code in (200, 406)
    except Exception:
        return False


@pytest.mark.skipif(not _rhea_up(), reason="needs a live rhea MCP server at :3001")
def test_synthesize_then_persist_real_muscle_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("RHEA_MCP_URL", _RHEA_URL)
    from nanobrain.library.steps.rhea_file_tool_step import RheaFileToolStep
    from nanobrain.library.tools.rhea_step_synthesizer import synthesize_rhea_step

    spec = asyncio.run(
        asyncio.wait_for(
            synthesize_rhea_step(
                "muscle",
                mcp_url=_RHEA_URL,
                find_tools_query="muscle multiple sequence alignment",
                static_tool_args={"diags": False},
            ),
            timeout=60,
        )
    )
    assert spec.is_pinned
    path = persist_rhea_step(spec, dest_dir=tmp_path)
    # The committed wrapper loads via its declared class with mcp_url resolved
    # from the env (portable) — proving the persisted step is actually usable.
    step = RheaFileToolStep.from_config(str(path))
    assert step is not None
