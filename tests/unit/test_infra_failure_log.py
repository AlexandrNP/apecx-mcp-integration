"""W3 — InfraFailureLog: append-only JSONL round-trip + size cap (bounded under a daemon)."""

from __future__ import annotations

from apecx_integration.infrastructure.failure_log import FailureEvent, InfraFailureLog


def test_record_and_recent_roundtrip(tmp_path):
    log = InfraFailureLog(tmp_path / "f.jsonl")
    log.record(FailureEvent(timestamp_iso="t1", component="redis", state="down", detail="x"))
    log.record(
        FailureEvent(
            timestamp_iso="t2",
            component="minio",
            state="degraded",
            detail="y",
            reload_attempted=True,
            reload_outcome="reload → ready",
        )
    )
    recent = log.recent()
    assert [r["component"] for r in recent] == ["redis", "minio"]
    assert recent[1]["reload_outcome"] == "reload → ready"
    assert recent[1]["reload_attempted"] is True


def test_recent_is_empty_when_no_file(tmp_path):
    assert InfraFailureLog(tmp_path / "nope.jsonl").recent() == []


def test_size_cap_keeps_last_n(tmp_path):
    log = InfraFailureLog(tmp_path / "f.jsonl", max_records=5)
    for i in range(20):
        log.record(FailureEvent(timestamp_iso=str(i), component="c", state="down", detail=""))
    lines = (tmp_path / "f.jsonl").read_text().splitlines()
    assert len(lines) == 5
    assert log.recent(limit=100)[0]["timestamp_iso"] == "15"  # oldest of the kept window
