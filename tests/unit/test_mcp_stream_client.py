"""Unit coverage for the desktop streaming client's PURE logic (E3-5).

The wire round-trip is proven by ``tests/integration/test_mcp_stream_client.py``
(a real stdio client↔server run). This file pins the client's transport-free pieces
so a regression in result parsing / stage extraction is caught without the 6-10 min
gated integration run: ``_result_to_dict`` (structured-vs-text-fallback) and
``StreamRun.stage_reports`` (filter log notifications down to stage reports). Mocking
the CallToolResult shape here is the carve-out's "pure dict-in/dict-out" case — the
matching real-dependency coverage is the integration test named above.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import mcp_stream_client as client  # noqa: E402


def test_result_to_dict_prefers_structured_content():
    result = CallToolResult(
        content=[TextContent(type="text", text='{"status": "from_text"}')],
        structuredContent={"status": "ok", "run_id": "r1"},
    )
    assert client._result_to_dict(result) == {"status": "ok", "run_id": "r1"}


def test_result_to_dict_falls_back_to_json_text():
    result = CallToolResult(
        content=[TextContent(type="text", text='{"status": "ok", "run_id": "r2"}')],
        structuredContent=None,
    )
    assert client._result_to_dict(result) == {"status": "ok", "run_id": "r2"}


def test_result_to_dict_reports_loudly_on_unparseable_content():
    result = CallToolResult(
        content=[TextContent(type="text", text="not json at all")],
        structuredContent=None,
    )
    out = client._result_to_dict(result)
    assert "error" in out and out["raw"]


@pytest.fixture
def run_with_mixed_events() -> client.StreamRun:
    run = client.StreamRun()
    run.events = [
        client.StreamEvent(seq=1, kind="progress", stage="a", payload={"progress": 1.0}),
        client.StreamEvent(
            seq=2,
            kind="log",
            stage="a",
            payload={"event": "stage_report", "stage": "a", "order": 0, "markdown": "A"},
        ),
        # A non-stage log notification (e.g. an incidental server log) is ignored.
        client.StreamEvent(seq=3, kind="log", stage=None, payload={"event": "other"}),
        client.StreamEvent(
            seq=4,
            kind="log",
            stage="b",
            payload={"event": "stage_report", "stage": "b", "order": 1, "markdown": "B"},
        ),
    ]
    return run


def test_stage_reports_filters_to_stage_events_in_arrival_order(run_with_mixed_events):
    reports = run_with_mixed_events.stage_reports

    assert [r["stage"] for r in reports] == ["a", "b"]
    # The discriminator key is stripped; the rest of the payload is preserved.
    assert all("event" not in r for r in reports)
    assert reports[0] == {"stage": "a", "order": 0, "markdown": "A"}


def test_progress_and_log_channels_split_cleanly(run_with_mixed_events):
    assert [e.seq for e in run_with_mixed_events.progress_events] == [1]
    assert [e.seq for e in run_with_mixed_events.log_events] == [2, 3, 4]
