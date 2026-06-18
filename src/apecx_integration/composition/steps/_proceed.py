"""How-to-proceed guidance — turn a degradation into a diagnosis + recommended next action.

Companion to ``_stage_report.py``. Every degrade point in the pipeline (single clade, too few
sequences, no structural hit, …) appends a structured note; the terminal synthesis renders the
accumulated notes as a single ``## How to proceed`` section, so a run that stopped short tells the
user WHAT was missing, WHY, and WHAT to do next — instead of dead-ending.

A proceed note is a dict::

    {"stage": str, "what": str, "why": str, "action": str, "severity": str}

``severity`` ∈ {"blocked", "low_confidence", "info"} and controls render order
(blocked → low_confidence → info). ``info`` is for POSITIVE findings too (e.g. a homogeneous
species is broadly effective by construction) — not every note is a problem.
"""

from __future__ import annotations

from typing import Any

_KEY = "proceed_notes"
_SEVERITY_ORDER = {"blocked": 0, "low_confidence": 1, "info": 2}
_SEVERITY_LABEL = {"blocked": "Blocked", "low_confidence": "Low confidence", "info": "Note"}


def append_proceed_note(
    bundle: dict[str, Any],
    *,
    stage: str,
    what: str,
    why: str,
    action: str,
    severity: str = "info",
) -> dict[str, Any]:
    """Append a how-to-proceed note to ``bundle['proceed_notes']`` and return the bundle.

    Copy-on-write (mirrors ``append_stage_report``): the existing list is COPIED before appending
    so an upstream step's aliased list is never mutated in place.

    Loud on bad input — empty ``stage``/``what``/``action`` or an unknown ``severity`` is a
    silent-failure shape (guidance that documents nothing), so it raises.
    """
    for field_name, val in (("stage", stage), ("what", what), ("action", action)):
        if not isinstance(val, str) or not val.strip():
            raise ValueError(
                f"append_proceed_note: {field_name!r} must be a non-empty string, got {val!r}"
            )
    if severity not in _SEVERITY_ORDER:
        raise ValueError(
            f"append_proceed_note: severity must be one of {sorted(_SEVERITY_ORDER)}, got {severity!r}"
        )
    existing = bundle.get(_KEY)
    notes = list(existing) if isinstance(existing, list) else []
    notes.append(
        {
            "stage": stage.strip(),
            "what": what.strip(),
            "why": why.strip() if isinstance(why, str) else "",
            "action": action.strip(),
            "severity": severity,
        }
    )
    bundle[_KEY] = notes
    return bundle


def render_how_to_proceed(bundle: dict[str, Any]) -> str:
    """Render accumulated proceed notes as a ``## How to proceed`` Markdown section.

    Ordered blocked → low_confidence → info, then insertion order for ties. Returns ``""`` when
    there are no notes so the caller omits the section entirely (no empty heading).
    """
    notes = bundle.get(_KEY) if isinstance(bundle, dict) else None
    valid = [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []
    if not valid:
        return ""
    ordered = sorted(valid, key=lambda n: _SEVERITY_ORDER.get(n.get("severity"), 99))
    lines = ["## How to proceed", ""]
    for n in ordered:
        label = _SEVERITY_LABEL.get(n.get("severity"), "Note")
        stage = n.get("stage") or "(stage)"
        what = (n.get("what") or "").strip()
        why = (n.get("why") or "").strip()
        action = (n.get("action") or "").strip()
        why_frag = f" {why}" if why else ""
        lines.append(f"- **{label} — {stage}:** {what}.{why_frag} _Next:_ {action}")
    return "\n".join(lines)


__all__ = ["append_proceed_note", "render_how_to_proceed"]
