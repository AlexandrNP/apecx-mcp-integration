"""Shared evidence-bundle resolver for the downstream assessment steps.

``conserved_epitope_candidate_assessment`` and ``epitope_combination_feasibility_assessment``
both accept an evidence source as EITHER a resolvable handle string OR an inline bundle, and
both must reduce it to a ``parts`` dict. This one helper is the single source of that logic
(the two steps previously duplicated it) and, critically, gives an ACTIONABLE error on the
one shape callers most often pass by mistake: the keys-only ``data_preview``.
"""

from __future__ import annotations

from typing import Any

from apecx_integration.composition.handles.store import default_handle_store
from apecx_integration.composition.schemas.data_shapes import Bundle


def evidence_bundle_parts(raw: Any, *, ctx: str) -> dict[str, Any]:
    """Resolve an evidence source to its ``parts`` dict. ``ctx`` names the caller for errors.

    Accepts a handle string (resolved via the durable handle store), a ``Bundle``, a
    ``{"kind": "bundle", "parts": {...}}`` dict, or a bare parts dict.

    Rejects the keys-only ``data_preview`` shape (``{"kind": "bundle", "parts": [<names>]}``)
    with an actionable message. That preview is what the upstream tool surfaces to the LLM
    alongside the opaque ``data_handle``; when the handle can't be resolved the LLM tends to
    pass the visible preview instead — which carries key NAMES, not the data. Point it back at
    the resolvable handle rather than raising a cryptic "parts must be a dict".
    """
    if isinstance(raw, str):
        shape = default_handle_store().get(raw.strip())
        if not isinstance(shape, Bundle):
            raise ValueError(
                f"{ctx}: handle must resolve to a Bundle DataShape, got {type(shape).__name__}."
            )
        return dict(shape.parts)
    if isinstance(raw, Bundle):
        return dict(raw.parts)
    if not isinstance(raw, dict):
        raise ValueError(
            f"{ctx}: evidence must be a handle string, a Bundle, or a parts dict, "
            f"got {type(raw).__name__}."
        )
    if raw.get("kind") == "bundle":
        parts = raw.get("parts")
        if isinstance(parts, list):
            raise ValueError(
                f"{ctx}: received the keys-only data_preview (its 'parts' is a list of key "
                "names, not the evidence). Pass the resolvable evidence_data_handle (the "
                "data_handle returned by the upstream run) instead of data_preview."
            )
        if not isinstance(parts, dict):
            raise ValueError(f"{ctx}: bundle.parts must be a dict.")
        return dict(parts)
    return dict(raw)
