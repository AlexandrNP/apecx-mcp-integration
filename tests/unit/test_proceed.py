"""Unit tests for the how-to-proceed degrade-guidance helper (_proceed.py)."""

from __future__ import annotations

import pytest

from apecx_integration.composition.steps._proceed import (
    append_proceed_note,
    render_how_to_proceed,
)


def test_append_and_render_single_note():
    b = append_proceed_note(
        {},
        stage="clade grouping",
        what="single clade",
        why="median pairwise identity 99%",
        action="supply more divergent strains",
        severity="info",
    )
    md = render_how_to_proceed(b)
    assert md.startswith("## How to proceed")
    assert "single clade" in md
    assert "median pairwise identity 99%" in md
    assert "supply more divergent strains" in md


def test_render_orders_blocked_then_low_confidence_then_info():
    b: dict = {}
    append_proceed_note(b, stage="s1", what="an info note", why="", action="a1", severity="info")
    append_proceed_note(
        b, stage="s2", what="a blocked note", why="", action="a2", severity="blocked"
    )
    append_proceed_note(
        b, stage="s3", what="a lowconf note", why="", action="a3", severity="low_confidence"
    )
    md = render_how_to_proceed(b)
    assert md.index("a blocked note") < md.index("a lowconf note") < md.index("an info note")


def test_render_empty_returns_empty_string():
    assert render_how_to_proceed({}) == ""
    assert render_how_to_proceed({"proceed_notes": []}) == ""
    assert render_how_to_proceed({"proceed_notes": "not a list"}) == ""


def test_append_is_copy_on_write():
    original: list = []
    b = {"proceed_notes": original}
    append_proceed_note(b, stage="s", what="w", why="", action="a")
    assert original == []  # upstream list object untouched
    assert len(b["proceed_notes"]) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"stage": "", "what": "w", "action": "a"},
        {"stage": "s", "what": "", "action": "a"},
        {"stage": "s", "what": "w", "action": ""},
    ],
)
def test_loud_on_empty_required_fields(bad):
    with pytest.raises(ValueError):
        append_proceed_note({}, why="", severity="info", **bad)


def test_loud_on_unknown_severity():
    with pytest.raises(ValueError, match="severity"):
        append_proceed_note({}, stage="s", what="w", why="", action="a", severity="critical")
