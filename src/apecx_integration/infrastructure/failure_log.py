"""Durable, size-capped JSONL sink for infrastructure failure events (dashboard / W3).

Infra failures are NOT run-scoped, so the run-scoped ``ProvenanceRecorder`` does not fit. This is a
plain append-only JSONL under ``~/.apecx/``, capped to the last ``max_records`` so a long-lived monitor
daemon cannot grow it without bound (the long-lived-server discipline). The caller supplies the
timestamp (no wall-clock call in here), keeping this deterministic + testable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".apecx" / "infra_failures.jsonl"
_MAX_RECORDS = 2000


@dataclass(frozen=True)
class FailureEvent:
    """One recorded infrastructure failure (optionally, its auto-reload outcome)."""

    timestamp_iso: str
    component: str
    state: str
    detail: str
    reload_attempted: bool = False
    reload_outcome: str = ""


class InfraFailureLog:
    def __init__(self, path: Path | str | None = None, *, max_records: int = _MAX_RECORDS) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_PATH
        self._max = max_records

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: FailureEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(asdict(event)) + "\n")
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        # Bound the file to the last _max records — an append-only file under a daemon would grow
        # without limit otherwise. Rewrites only when actually over the cap.
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        if len(lines) > self._max:
            self._path.write_text("\n".join(lines[-self._max :]) + "\n", encoding="utf-8")

    def recent(self, limit: int = 50) -> list[dict]:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out: list[dict] = []
        for raw in lines[-limit:]:
            line = raw.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
