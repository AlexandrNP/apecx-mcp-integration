"""Dashboard routes — live infrastructure health + recent failures (W3.2).

``GET /status``    → JSON ``{overall, backends[], recent_failures[]}`` over the monitor's shared state.
                     The snapshot is refreshed on request (``InfraMonitor.snapshot``) so this works with
                     OR without the always-on daemon — the daemon adds the auto-reload + recording.
``GET /dashboard`` → a minimal auto-refresh HTML view over the same data.

Both read the SAME ``InfraMonitor`` singleton the CLI ``apecx-dashboard`` polls, so every view shows
one consistent state.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from apecx_integration.infrastructure.monitor import get_monitor

router = APIRouter(tags=["dashboard"])

_REFRESH_S = 5


@router.get("/status")
async def status() -> dict[str, Any]:
    monitor = get_monitor()
    snap = await monitor.snapshot()
    return {
        "overall": snap.get("overall"),
        "backends": snap.get("backends", []),
        "recent_failures": monitor.recent_failures(),
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return _render_html(await status())


def _dot(reachable: bool, state: str) -> str:
    if state in ("ready", "reused"):
        return "●"
    if not reachable:
        return "○"  # genuinely down
    return "◐"  # up but degraded


def _render_html(data: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{b.get('name', '')}</td>"
        f"<td>{_dot(b.get('reachable', True), b.get('state', ''))} {b.get('state', '')}</td>"
        f"<td>{b.get('detail', '')}</td></tr>"
        for b in data.get("backends", [])
    )
    fails = "".join(
        f"<li>{f.get('timestamp_iso', '')} — {f.get('component', '')} {f.get('state', '')}"
        f"{' → ' + f['reload_outcome'] if f.get('reload_outcome') else ''}</li>"
        for f in data.get("recent_failures", [])[-15:]
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{_REFRESH_S}">
<title>apecx-mcp infra</title>
<style>body{{font-family:monospace;margin:2rem}}table{{border-collapse:collapse}}
td,th{{padding:.2rem .8rem;border-bottom:1px solid #ddd;text-align:left}}h2{{margin-top:1.5rem}}</style>
</head><body>
<h1>apecx-mcp infrastructure — overall: {data.get("overall", "?")}</h1>
<table><tr><th>component</th><th>state</th><th>detail</th></tr>{rows}</table>
<h2>recent failures</h2><ul>{fails or "<li>(none recorded)</li>"}</ul>
<p><small>auto-refresh {_REFRESH_S}s · JSON at <a href="/status">/status</a></small></p>
</body></html>"""
