"""E3-5 — the desktop streaming split, proven END-TO-END over a REAL stdio MCP wire.

E2-S shipped ``run_workflow_streaming`` and tested it only with a fake ``Context``:
no real MCP client had ever consumed the stream. That left an honest gap — a
"tests pass, product maybe broken" shape, because a fake Context cannot exercise
the actual notification serialization, the stdio transport, or a real client's
``logging_callback`` / ``progress_callback`` plumbing.

This test closes it: ``scripts/mcp_stream_client.py`` launches ``apecx-mcp`` as a
subprocess, does the MCP handshake, registers a real log handler + progress
handler, and drives ``run_workflow_streaming`` over the wire. NO MOCK of the MCP
transport — it is a genuine client↔server stdio round-trip (the whole point).

ROBUSTNESS (E3-S-followup): the reference client now calls
``session.set_logging_level("info")`` during init — the standards-compliant path
a real desktop MCP client (Claude Desktop) takes. Before the fix that raised
``McpError: Method not found`` and tore the session down BEFORE the streaming
tool ran; the server now advertises + handles the MCP ``logging`` capability, so
the call succeeds and the full stream still arrives. If the fix regressed, this
test would not receive any stages (the session would be dead) — so the stage
assertions below double as the regression guard for the capability fix.

Asserts (CC-1, real data — not "didn't crash"):
  (a) the client receives >=6 stage notifications, IN ORDER, covering the real
      stages (data_readiness, structural_evidence, sequence_conservation,
      structural_reasoning, functional_validation);
  (b) the streamed stage reports EQUAL the reasoning-trace content in the final
      headless document (streamed == headless, no divergence over the wire);
  (c) the final result carries the 5-section output contract + status=ok.

Reuses the existing reachability gate (``needs_llm_seq``) and query fixtures from
``test_viral_epitope_evidence_review`` — the prerequisites are identical (LLM +
Globus + MAFFT + BV-BRC), so re-deriving them here would duplicate knowledge.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.integration.test_viral_epitope_evidence_review import (
    _CHIKV_TAXON,
    _QUERY,
    needs_llm_seq,
)

pytestmark = pytest.mark.integration

# scripts/ is not a package; add it to the path so the client module imports.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_REAL_STAGES = {
    "data_readiness",
    "structural_evidence",
    "sequence_conservation",
    "structural_reasoning",
    "functional_validation",
}


@needs_llm_seq
def test_real_stdio_client_consumes_streamed_stages_e2e():
    """A REAL stdio MCP client drives ``run_workflow_streaming`` end-to-end and the
    streamed stages match the headless document — proving the desktop split is wire-
    deep, not test-harness-deep."""
    import mcp_stream_client as client

    run = asyncio.run(
        client.stream_workflow(
            _QUERY,
            params_extra={"taxon_id": _CHIKV_TAXON, "protein": "structural polyprotein"},
            timeout_minutes=20,
        )
    )

    # The client's own callbacks must not have failed (reliability: a handler bug
    # would be recorded here rather than tearing the session down).
    assert run.handler_errors == [], run.handler_errors

    stage_reports = run.stage_reports
    arrival = [r["stage"] for r in stage_reports]
    print("\nREAL-CLIENT STREAMED STAGE ARRIVAL ORDER:", arrival)
    print(
        "PROGRESS NOTIFICATIONS:", [(e.payload["progress"], e.stage) for e in run.progress_events]
    )

    # (a) CC-1: >=6 non-empty stage notifications arrived over the wire, covering the
    # real stages, each carrying real markdown (assert content, not just count).
    assert len(stage_reports) >= 6, arrival
    assert all((r.get("markdown") or "").strip() for r in stage_reports), stage_reports
    assert set(arrival) >= _REAL_STAGES, (_REAL_STAGES - set(arrival), arrival)
    assert len(arrival) == len(set(arrival)), ("a stage was streamed twice", arrival)

    # Step-completion order is monotonic through the DAG (same ordering E2-S proved,
    # now observed over the real transport).
    idx = {s: arrival.index(s) for s in _REAL_STAGES}
    assert (
        idx["data_readiness"]
        < idx["structural_evidence"]
        < idx["sequence_conservation"]
        < idx["structural_reasoning"]
        < idx["functional_validation"]
    ), idx

    # Every stage's progress notification accompanied its log notification (the
    # server emits both per stage). Progress counter is strictly increasing.
    progress_values = [e.payload["progress"] for e in run.progress_events]
    assert len(progress_values) >= 6, progress_values
    assert progress_values == sorted(progress_values), progress_values
    assert len(set(progress_values)) == len(progress_values), progress_values

    # (c) the returned envelope satisfies the 5-section output contract + status=ok.
    result = run.result or {}
    assert result.get("status") == "ok", result
    assert result.get("error") is None
    assert result.get("run_id")
    md = result["markdown"]
    headers = [
        "# Answer",
        "## Cross-data reasoning",
        "## Integrated insight",
        "## Sources and evidence",
        "## Follow-up questions",
    ]
    positions = [md.find(h) for h in headers]
    assert all(p != -1 for p in positions), (positions, md[:3000])
    assert positions == sorted(positions), positions

    # (b) streamed == headless: rendering the OVER-THE-WIRE stage reports reproduces
    # the final document's reasoning-trace block verbatim. Same stage_reports, same
    # deterministic render — no divergence introduced by the MCP transport.
    from apecx_integration.composition.steps._stage_report import render_stage_reports

    rendered = render_stage_reports({"stage_reports": stage_reports})
    print("\nRENDERED-FROM-WIRE TRACE:\n", rendered)
    assert "### Reasoning trace" in md
    assert rendered in md, (
        "streamed-over-the-wire stage reports diverge from the headless document",
        rendered,
        md[md.find("### Reasoning trace") : md.find("### Reasoning trace") + 1500],
    )
