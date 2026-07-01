"""W3.2 — apecx-dashboard CLI: render() table + --once flow + unreachable-CP handling."""

from __future__ import annotations

from apecx_integration.cli import dashboard

_SNAP = {
    "overall": "degraded",
    "backends": [
        {"name": "redis", "state": "ready", "reachable": True, "detail": ":6379"},
        {"name": "ollama", "state": "degraded", "reachable": True, "detail": "no model"},
        {"name": "minio", "state": "down", "reachable": False, "detail": "refused"},
    ],
    "recent_failures": [
        {
            "timestamp_iso": "2026-07-01T10:00:00",
            "component": "minio",
            "state": "down",
            "reload_outcome": "reload → ready",
        }
    ],
}


def test_render_shows_components_states_and_dots():
    out = dashboard.render(_SNAP)
    assert "overall: degraded" in out
    assert "redis" in out and "ollama" in out and "minio" in out
    assert "● ready" in out  # healthy
    assert "○ down" in out  # genuinely down (unreachable)
    assert "◐ degraded" in out  # up but degraded
    assert "reload → ready" in out


def test_main_once_prints_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(dashboard, "_fetch_status", lambda url, timeout=5.0: _SNAP)
    rc = dashboard.main(["--once", "--url", "http://x"])
    assert rc == 0
    assert "overall: degraded" in capsys.readouterr().out


def test_main_once_handles_unreachable_control_plane(monkeypatch, capsys):
    def _boom(url, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr(dashboard, "_fetch_status", _boom)
    rc = dashboard.main(["--once", "--url", "http://x"])
    assert rc == 0
    assert "unreachable" in capsys.readouterr().out
