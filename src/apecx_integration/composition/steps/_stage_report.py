"""Stage-report scaffolding for the viral_epitope_analysis pipeline.

A reusable convention: every reasoning stage appends a documented sub-report to a
``stage_reports`` list carried in the bundle dict that flows step-to-step. The
terminal synthesis renders the accumulated reports as a ``### Reasoning trace``
inside the cross-data-reasoning section, so the final document carries a
transparent, ordered record of what each stage contributed.

This is the plug-in point for the future reasoning stages (data-readiness,
sequence, functional, …): a new stage step calls ``append_stage_report`` from its
``process()`` and the trace renders it automatically — no change to the synthesis
step is needed.

A stage report is a dict::

    {"stage": str, "order": int, "markdown": str, "data": dict}

``order`` controls render position (ascending); ``markdown`` is a short
human-readable fragment; ``data`` is machine-readable detail (counts, notes) for
future programmatic consumers (telemetry, quality gates).
"""

from __future__ import annotations

from typing import Any

_KEY = "stage_reports"


def append_stage_report(
    bundle: dict[str, Any],
    stage: str,
    order: int,
    markdown: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a stage report to ``bundle['stage_reports']`` and return the bundle.

    Copy-on-write: the existing list is COPIED before appending so an upstream
    step's list object (aliased into this bundle through a ``dict(input_data)``
    shallow copy) is never mutated in place. The bundle dict itself is mutated
    (the ``stage_reports`` key is (re)set to the new list).

    Loud on bad input — a stage with no name or an empty markdown fragment is a
    silent-failure shape (a trace entry that documents nothing), so it raises.
    """
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError(f"append_stage_report: 'stage' must be a non-empty string, got {stage!r}")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError(
            f"append_stage_report: stage {stage!r} 'markdown' must be a non-empty string, "
            f"got {type(markdown).__name__}"
        )
    existing = bundle.get(_KEY)
    reports = list(existing) if isinstance(existing, list) else []
    reports.append(
        {
            "stage": stage.strip(),
            "order": int(order),
            "markdown": markdown.strip(),
            "data": dict(data) if isinstance(data, dict) else {},
        }
    )
    bundle[_KEY] = reports
    return bundle


def render_stage_reports(bundle: dict[str, Any]) -> str:
    """Render the accumulated stage reports as a Markdown fragment.

    Ordered by ``order`` (ascending), then by insertion order for ties. Returns an
    explicit placeholder when no reports exist so the reasoning-trace subsection is
    never silently empty.
    """
    reports = bundle.get(_KEY) if isinstance(bundle, dict) else None
    if not isinstance(reports, list) or not reports:
        return "_No stage reports were recorded for this run._"
    ordered = sorted(
        (r for r in reports if isinstance(r, dict)),
        key=lambda r: r.get("order", 0),
    )
    lines: list[str] = []
    for r in ordered:
        stage = r.get("stage") or "(unnamed stage)"
        frag = (r.get("markdown") or "").strip().replace("\n", " ")
        lines.append(f"- **{stage}** — {frag}")
    return "\n".join(lines)


__all__ = ["append_stage_report", "render_stage_reports"]
