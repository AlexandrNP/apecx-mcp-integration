"""G127 (EF7): the run_workflow honesty check must NOT false-flag a degrade-loud NESTED step failure that
its top-level parent caught (the cascade still produced an envelope). A nested ``muscle_alignment`` failure
inside the degrade-loud rhea_genomic leg was reporting the whole run as failed + blocking the artifact write.

Pure-logic tests of ``_flagged_step_failures`` (the extracted classifier). The contract:
  • a TOP-LEVEL step failure always counts (an uncaught nested failure propagates to its top-level parent,
    which then also appears as failed);
  • a NESTED-only failure counts ONLY when no envelope was produced (the cascade stalled = silent failure);
  • unknown top-level set → strict (flag all), never weaken the guard blindly.
"""

from __future__ import annotations

from apecx_integration.mcp_surface.tools.eo_primitives import _flagged_step_failures

_TOP = {"normalize", "sequence", "rhea_genomic", "envelope"}


def test_no_failures():
    assert _flagged_step_failures(_TOP, [], True) == []


def test_top_level_failure_always_flagged():
    assert _flagged_step_failures(_TOP, ["rhea_genomic"], True) == ["rhea_genomic"]
    assert _flagged_step_failures(_TOP, ["rhea_genomic"], False) == ["rhea_genomic"]


def test_nested_failure_with_envelope_is_degraded_not_flagged():
    # EF7: muscle_alignment (nested) raised, rhea_genomic (top-level parent) CAUGHT it, envelope produced.
    assert _flagged_step_failures(_TOP, ["muscle_alignment"], True) == []


def test_nested_failure_without_envelope_stalled_is_flagged():
    # a nested failure that STALLED the cascade (no output) IS the silent failure the guard exists for.
    assert _flagged_step_failures(_TOP, ["muscle_alignment"], False) == ["muscle_alignment"]


def test_top_level_plus_nested_flags_top_level():
    assert _flagged_step_failures(_TOP, ["sequence", "muscle_alignment"], True) == ["sequence"]


def test_unknown_top_level_is_strict():
    # cannot classify nesting → preserve the strict pre-fix guard (flag everything).
    assert set(_flagged_step_failures(set(), ["muscle_alignment"], True)) == {"muscle_alignment"}


def test_real_viral_epitope_analysis_wiring():
    """Pin the LOAD-BEARING wiring (review finding): in the REAL workflow, the degrade-loud rhea_genomic
    leg is a TOP-LEVEL step and the nested ``muscle_alignment`` is NOT — built exactly as run_workflow
    derives top_level (step_id keys ∪ each child's .name). So a caught muscle_alignment failure with an
    envelope classifies as nested → not flagged (EF7), while a genuine top-level failure IS flagged. No
    network/run — construction only."""
    from apecx_integration.composition.workflows.viral_epitope_analysis.builder import (
        build_viral_epitope_analysis_workflow,
    )
    from apecx_integration.mcp_surface.tools.eo_primitives import _top_level_step_names

    wf = build_viral_epitope_analysis_workflow()
    top_level = _top_level_step_names(wf)  # the SAME derivation run_workflow uses (shared helper)
    assert "rhea_genomic" in top_level  # the degrade-loud parent IS a direct child
    assert "muscle_alignment" not in top_level  # the nested rhea step is NOT
    # EF7: nested muscle_alignment failed, rhea_genomic caught it, envelope produced → NOT flagged
    assert _flagged_step_failures(top_level, ["muscle_alignment"], True) == []
    # a genuine top-level failure is still flagged (guard intact)
    assert _flagged_step_failures(top_level, ["envelope"], True) == ["envelope"]
    # a nested failure that STALLED (no envelope) is still flagged
    assert _flagged_step_failures(top_level, ["muscle_alignment"], False) == ["muscle_alignment"]
