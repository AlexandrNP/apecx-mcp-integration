"""Deployment e2e — REAL workflow runs to completion via the run_workflow TOOL (the true end-to-end).

This is the check the prose runbook could never make: it actually RUNS workflows against the live LLM
through the real MCP dispatch and asserts on the produced report.

F9 (deployment finding, encoded here): the registered `run_workflow` TOOL returns the finished report
as desktop-presentation TEXT (a content block), NOT the `{status, markdown, run_id}` JSON envelope that
the internal `run_workflow(...)` function returns. So the success signal on the real tool path is a
substantial, on-topic report (G127 — assert on the VALUE, and here the value IS the report text).

Gated on a reachable ollama with a model; slow by nature (real LLM + real data fetches).
"""

from __future__ import annotations


def _report_text(payload) -> str:
    # The tool result is either the presentation text (`_text`) or, if some path returns the envelope,
    # its `markdown`. Either way the report body is what a client renders.
    return payload.get("_text") or payload.get("markdown") or ""


def _assert_real_report(payload, workflow, must_mention):
    err = payload.get("error")
    assert not err, f"{workflow} errored: {err}"
    status = payload.get("status")  # present only if the envelope leaked through
    assert status in (None, "ok", "partial"), f"{workflow} status={status!r}"
    report = _report_text(payload)
    assert len(report) > 400, (
        f"{workflow} report suspiciously short ({len(report)} chars): {report[:200]!r}"
    )
    low = report.lower()
    assert must_mention in low, (
        f"{workflow} report never mentions {must_mention!r}: {report[:300]!r}"
    )
    return report


def test_run_rag_e2e_synthesis(call, ollama_or_skip):
    payload = call(
        "run_workflow",
        {"name": "rag_e2e_synthesis", "params": {"query": "What is chikungunya virus?"}},
    )
    _assert_real_report(payload, "rag_e2e_synthesis", must_mention="chikungunya")


def test_run_viral_epitope_analysis(call, ollama_or_skip, dict_or_skip):
    payload = call(
        "run_workflow",
        {
            "name": "viral_epitope_analysis",
            "params": {"query": "conserved epitopes on chikungunya virus E1 glycoprotein"},
        },
    )
    _assert_real_report(payload, "viral_epitope_analysis", must_mention="chikungunya")
