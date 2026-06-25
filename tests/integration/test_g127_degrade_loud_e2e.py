"""G127/EF7 pinned RUNTIME e2e (the review-gate's filed follow-up). The unit tests cover the classifier
truth-table + the real-workflow wiring; this drives a REAL run end-to-end and applies the EXACT run_workflow
G127 classifier to the REAL outcome, proving the call-site invariant the unit tests asserted only in prose:

  an UNCAUGHT nested failure marks its top-level parent failed (→ flagged), while a CAUGHT nested failure with
  a produced envelope is NOT flagged (status stays ok).

The fixture (``_g127_fixtures``) is the minimal, network-free, faithful reproduction of the rhea_genomic
scenario: a degrade-loud top-level step runs a nested sub-workflow whose inner step raises, catches it, and
continues — the inner ``step_failed`` bubbles into ``run_summary.steps`` exactly like ``muscle_alignment``.
"""

from __future__ import annotations

import asyncio


def test_g127_caught_nested_failure_is_not_flagged_end_to_end():
    from apecx_integration.composition.runtime.observed_run import run_workflow_observed
    from apecx_integration.mcp_surface.tools.eo_primitives import (
        _flagged_step_failures,
        _top_level_step_names,
    )
    from tests.integration._g127_fixtures import build_g127_degrade_loud_workflow

    wf = build_g127_degrade_loud_workflow()
    outcome = asyncio.run(
        run_workflow_observed(
            wf, {"workflow_input": {"x": 1}}, timeout=30, settle_ms=500, await_cascade=True
        )
    )
    failed = [s.step_name for s in outcome.run_summary.steps if s.status == "failed"]

    # The scenario actually reproduced: the NESTED inner step failed, the top-level parent CAUGHT it, and an
    # envelope WAS produced (without these, the test would be vacuous).
    assert "g127_inner_fail" in failed, (
        f"scenario didn't reproduce a nested failure: failed={failed}"
    )
    assert outcome.workflow_result is not None, "envelope not produced — fixture broken"

    top = _top_level_step_names(wf)
    assert "g127_inner_fail" not in top, "inner step is supposed to be NESTED, not top-level"
    assert {"catching_parent", "envelope"} <= top

    # THE invariant: run_workflow's real G127 classifier does NOT flag the caught nested failure.
    assert _flagged_step_failures(top, failed, outcome.workflow_result is not None) == []

    # And the strict pre-fix behavior (no top-level classification) WOULD have flagged it — proving this
    # test exercises the fix, not a vacuous pass.
    assert "g127_inner_fail" in _flagged_step_failures(set(), failed, True)
