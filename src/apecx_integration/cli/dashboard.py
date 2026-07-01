"""apecx-dashboard — live terminal view of apecx-mcp infrastructure health (W3.2).

A thin client of the control plane's ``GET /status`` (the SAME ``InfraMonitor`` state the web
``/dashboard`` shows). Renders a pure-string table (no ``rich`` dependency); ``--once`` prints a single
snapshot, otherwise it clears + re-renders every ``--interval`` seconds until Ctrl-C.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from typing import Any

_DEFAULT_URL = os.environ.get("APECX_CONTROL_PLANE_URL", "http://127.0.0.1:8000")


def _fetch_status(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(base_url.rstrip("/") + "/status", timeout=timeout) as resp:
        return json.loads(resp.read())


def _dot(b: dict[str, Any]) -> str:
    state = b.get("state", "")
    if state in ("ready", "reused"):
        return "●"
    if not b.get("reachable", True):
        return "○"  # genuinely down
    return "◐"  # up but degraded


def render(data: dict[str, Any]) -> str:
    lines = [
        f"apecx-mcp infrastructure — overall: {data.get('overall', '?')}",
        "-" * 68,
        f"{'COMPONENT':<16}{'STATE':<18}DETAIL",
    ]
    for b in data.get("backends", []):
        state_cell = f"{_dot(b)} {b.get('state', '')}"
        lines.append(f"{b.get('name', ''):<16}{state_cell:<18}{b.get('detail', '')[:40]}")
    fails = data.get("recent_failures", [])
    if fails:
        lines.append("-" * 68)
        for f in fails[-5:]:
            outcome = f" → {f['reload_outcome']}" if f.get("reload_outcome") else ""
            lines.append(
                f"⟳ {f.get('timestamp_iso', '')[:19]} {f.get('component', '')} "
                f"{f.get('state', '')}{outcome}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="apecx-dashboard", description="Live apecx-mcp infra health.")
    p.add_argument("--url", default=_DEFAULT_URL, help="control plane base URL")
    p.add_argument(
        "--interval", type=float, default=5.0, help="refresh seconds (ignored with --once)"
    )
    p.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = p.parse_args(argv)

    try:
        while True:
            try:
                out = render(_fetch_status(args.url))
            except Exception as exc:  # noqa: BLE001 — a down control plane is a normal displayed state
                out = (
                    f"apecx-dashboard: control plane unreachable at {args.url}/status — {exc}\n"
                    "(start it with: apecx-cp serve)"
                )
            if args.once:
                print(out)
                return 0
            print("\033[2J\033[H" + out, flush=True)  # clear screen + cursor home
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
