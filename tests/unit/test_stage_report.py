"""Unit tests for the stage-report scaffolding (E2-C).

The stage-report mechanism is the plug-in point future reasoning stages use to
contribute a documented sub-report. These pin the append/render round-trip,
ordering, copy-on-write (no upstream-list mutation), and the loud-on-bad-input
contract.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.steps._stage_report import (
    append_stage_report,
    render_stage_reports,
)


def test_append_then_render_round_trips():
    bundle: dict = {"query": "q"}
    append_stage_report(bundle, "context_assembly", 1, "Assembled 3 sources.", {"n": 3})
    append_stage_report(bundle, "structural_evidence", 2, "Found 2 structures.")
    reports = bundle["stage_reports"]
    assert [r["stage"] for r in reports] == ["context_assembly", "structural_evidence"]
    assert reports[0]["data"] == {"n": 3}
    assert reports[1]["data"] == {}  # default empty dict, never None

    rendered = render_stage_reports(bundle)
    assert "**context_assembly**" in rendered and "Assembled 3 sources." in rendered
    assert "**structural_evidence**" in rendered and "Found 2 structures." in rendered


def test_render_orders_by_order_field():
    bundle: dict = {}
    append_stage_report(bundle, "late", 9, "late frag")
    append_stage_report(bundle, "early", 1, "early frag")
    rendered = render_stage_reports(bundle)
    assert rendered.index("early frag") < rendered.index("late frag")


def test_copy_on_write_does_not_mutate_upstream_list():
    """A downstream step's ``dict(input_data)`` aliases the same list object; append
    must NOT mutate that upstream list in place (it copies first)."""
    upstream: dict = {}
    append_stage_report(upstream, "context_assembly", 1, "first")
    upstream_list = upstream["stage_reports"]

    downstream = dict(upstream)  # shallow copy → stage_reports aliased
    append_stage_report(downstream, "structural_evidence", 2, "second")

    assert len(upstream_list) == 1  # upstream untouched
    assert len(downstream["stage_reports"]) == 2


def test_render_empty_is_explicit_not_blank():
    assert "_No stage reports were recorded" in render_stage_reports({})


@pytest.mark.parametrize("bad_stage", ["", "   ", None])
def test_append_rejects_blank_stage(bad_stage):
    with pytest.raises(ValueError):
        append_stage_report({}, bad_stage, 1, "frag")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_md", ["", "   ", None])
def test_append_rejects_blank_markdown(bad_md):
    with pytest.raises(ValueError):
        append_stage_report({}, "stage", 1, bad_md)  # type: ignore[arg-type]
