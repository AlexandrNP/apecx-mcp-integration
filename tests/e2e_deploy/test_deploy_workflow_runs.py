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


def _report_body(payload) -> str:
    # The tool result is either the presentation text (`_text`) or, if a path returns the envelope, its
    # `markdown`. In DESKTOP locus (the default) the text is a ~907-char HOST_INSTRUCTIONS preamble +
    # "\n\n---\n\n" + the actual report — so we STRIP the preamble and measure the REPORT BODY, else a
    # length gate would be vacuously true on the preamble alone (review-gate note, F9).
    txt = payload.get("_text") or payload.get("markdown") or ""
    marker = "\n\n---\n\n"
    return txt.split(marker, 1)[1] if marker in txt else txt


def _assert_real_report(payload, workflow, must_mention):
    err = payload.get("error")
    assert not err, f"{workflow} errored: {err}"
    status = payload.get("status")  # present only if the envelope leaked through
    assert status in (None, "ok", "partial"), f"{workflow} status={status!r}"
    body = _report_body(payload)
    # Measure the report BODY (preamble stripped) so this is a real substance gate, not a check on the
    # fixed instruction preamble.
    assert len(body) > 400, f"{workflow} report body too short ({len(body)} chars): {body[:200]!r}"
    assert must_mention in body.lower(), (
        f"{workflow} report never mentions {must_mention!r}: {body[:300]!r}"
    )
    return body


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
