"""Catalog-grounded class-path resolver (CPR, 2026-05-11).

The dominant hallucination shape across both mistral-nemo and gemma4
on this composer task is "leaf class name correct, module path drifted":

  - emitted: ``apecx_integration.composition.steps.rag_synthesis.RagSynthesisStep``
  - actual:  ``apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep``
                                                                 ^^^^^

The LLM remembers the class name but invents a module path. This
resolver fixes that pattern deterministically — no extra LLM call,
no risk of substituting the wrong class — by anchoring on the leaf
class name (the last dotted segment).

Three outcomes per emitted class path:

  - ``exact`` — already in the catalog. No-op.
  - ``leaf_match`` — leaf matches exactly ONE catalog entry; rewrite.
  - ``ambiguous`` / ``novel`` — no unique match; defer to A1's
    ``step_class_unresolvable`` violation, with this resolver
    contributing "Did you mean: X?" hints into the validator's
    suggested_fix.

What the resolver deliberately does NOT do:

  - Fuzzy match across module paths. The Levenshtein temptation is
    real, but the cost of a wrong silent substitution (the LLM
    asked for X, got Y, the wrong step runs) is far worse than
    the cost of one extra retry. Leaf-name equality is the only
    auto-correction signal we trust.
  - Reach into the framework's import machinery. We operate purely
    on catalog dotted strings; the framework's own ``from_config``
    handles the actual import.

Framework-native: uses ``ComponentCatalog`` (the existing shipped
primitive). The repair surface is composer-side; the framework's
contract (``Workflow.from_config`` takes a class path and imports it)
is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResolutionKind(StrEnum):
    """How the resolver classifies an emitted class path."""

    EXACT = "exact"
    LEAF_MATCH = "leaf_match"
    AMBIGUOUS = "ambiguous"
    NOVEL = "novel"


@dataclass(frozen=True, kw_only=True)
class ClassPathResolution:
    """One resolver verdict.

    Fields:
        emitted: the class path the LLM produced.
        resolved: the catalog class path we'd substitute (when
            ``kind`` is ``exact`` or ``leaf_match``). ``None`` for
            ``ambiguous`` / ``novel``.
        kind: classification of the match.
        candidates: when ``kind`` is ``ambiguous``, the list of
            catalog paths whose leaf matches the emitted leaf. Used
            for "Did you mean?" hints in the validator's
            ``suggested_fix``.
    """

    emitted: str
    resolved: str | None
    kind: ResolutionKind
    candidates: tuple[str, ...] = ()


def _leaf(class_path: str) -> str:
    """Last dotted segment of a class path. Empty string for malformed."""
    if not class_path or "." not in class_path:
        return class_path or ""
    return class_path.rsplit(".", 1)[-1]


def resolve_class_path(
    emitted: str,
    catalog_class_paths: set[str],
) -> ClassPathResolution:
    """Classify an emitted class path against a catalog set.

    Args:
        emitted: the LLM-produced class path. Empty string handled
            as ``novel`` (the validator surfaces a different
            ``step_class_missing`` rule).
        catalog_class_paths: every dotted class path known to the
            composer — typically ``self._catalog.components``
            mapped to their ``class_path`` attribute. Passing only
            the retrieval hits would miss A2-rescued components.

    Returns:
        A ``ClassPathResolution`` describing the verdict. Never
        raises — pure inspection.
    """
    if not emitted:
        return ClassPathResolution(
            emitted=emitted,
            resolved=None,
            kind=ResolutionKind.NOVEL,
        )

    if emitted in catalog_class_paths:
        return ClassPathResolution(
            emitted=emitted,
            resolved=emitted,
            kind=ResolutionKind.EXACT,
        )

    emitted_leaf = _leaf(emitted)
    leaf_matches = sorted(cp for cp in catalog_class_paths if _leaf(cp) == emitted_leaf)
    if len(leaf_matches) == 1:
        return ClassPathResolution(
            emitted=emitted,
            resolved=leaf_matches[0],
            kind=ResolutionKind.LEAF_MATCH,
            candidates=tuple(leaf_matches),
        )
    if len(leaf_matches) >= 2:
        return ClassPathResolution(
            emitted=emitted,
            resolved=None,
            kind=ResolutionKind.AMBIGUOUS,
            candidates=tuple(leaf_matches),
        )

    # No leaf match. Surface "closest by leaf prefix/suffix" hints —
    # these go into the validator's suggested_fix when the rule fires.
    near = _near_leaf_matches(emitted_leaf, catalog_class_paths, limit=3)
    return ClassPathResolution(
        emitted=emitted,
        resolved=None,
        kind=ResolutionKind.NOVEL,
        candidates=near,
    )


def _near_leaf_matches(
    emitted_leaf: str,
    catalog_class_paths: set[str],
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """Catalog paths whose leaf shares a substring with the emitted leaf.

    Pure heuristic for "did you mean" hints — does NOT trigger
    auto-correction. We accept some false positives here because the
    hint is presented to a downstream reviewer (LLM in retry or human
    in approval), not silently applied.
    """
    if not emitted_leaf:
        return ()
    emitted_lower = emitted_leaf.lower()

    def _overlap(other_leaf: str) -> int:
        ol = other_leaf.lower()
        # Cheap "shared start or shared end" score; doesn't need
        # to be sophisticated, just better than random.
        score = 0
        for n in range(min(len(emitted_lower), len(ol)), 2, -1):
            if emitted_lower[:n] == ol[:n] or emitted_lower[-n:] == ol[-n:]:
                score = n
                break
        return score

    ranked = sorted(
        catalog_class_paths,
        key=lambda cp: _overlap(_leaf(cp)),
        reverse=True,
    )
    return tuple(cp for cp in ranked[:limit] if _overlap(_leaf(cp)) > 0)


# ---------------------------------------------------------------------------
# Workflow-level repair
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ClassPathRepair:
    """One auto-correction applied to a parsed workflow.

    Persisted on ``CompositionSummary.class_path_repairs`` so a
    reviewer reading the composition record can see exactly which
    paths the resolver rewrote. Operators tracking quality SELECT
    these to measure how often the LLM hallucinates the suffix-
    drop shape over time.
    """

    step_id: str
    emitted: str
    resolved: str
    kind: ResolutionKind


def repair_workflow_class_paths(
    workflow_dict: dict,
    catalog_class_paths: set[str],
) -> list[ClassPathRepair]:
    """Walk ``workflow_dict["steps"]`` and auto-correct any
    ``leaf_match`` resolutions in place.

    Returns:
        List of repairs applied. Empty when nothing needed fixing.
        ``ambiguous`` and ``novel`` resolutions are left alone so
        A1's ``step_class_unresolvable`` rule fires and C1 retry
        engages.

    The mutation IS in-place — callers that want to preserve the
    original parsed dict should pass a copy. Done this way because
    the composer's compose() already owns the workflow_dict
    lifetime and doesn't need a second buffer.
    """
    repairs: list[ClassPathRepair] = []
    steps = workflow_dict.get("steps")
    if not isinstance(steps, dict):
        return repairs

    for step_id, body in steps.items():
        if not isinstance(body, dict):
            continue
        emitted = body.get("class")
        if not isinstance(emitted, str) or not emitted:
            continue
        resolution = resolve_class_path(emitted, catalog_class_paths)
        if resolution.kind is ResolutionKind.LEAF_MATCH:
            body["class"] = resolution.resolved
            repairs.append(
                ClassPathRepair(
                    step_id=str(step_id),
                    emitted=emitted,
                    resolved=resolution.resolved or "",
                    kind=resolution.kind,
                )
            )
    return repairs


def hint_for_step_violation(
    emitted: str,
    catalog_class_paths: set[str],
) -> str | None:
    """Format a "Did you mean X?" hint for the validator's
    suggested_fix string.

    Returns ``None`` when no helpful suggestion exists. Used by the
    workflow_validator's ``step_class_unresolvable`` branch.
    """
    resolution = resolve_class_path(emitted, catalog_class_paths)
    if resolution.kind is ResolutionKind.AMBIGUOUS:
        listed = "; ".join(resolution.candidates)
        return f"Did you mean one of: {listed}?"
    if resolution.kind is ResolutionKind.NOVEL and resolution.candidates:
        listed = "; ".join(resolution.candidates)
        return (
            f"No catalog entry has the leaf name {_leaf(emitted)!r}. "
            f"Closest catalog entries by name: {listed}."
        )
    return None


__all__ = [
    "ClassPathRepair",
    "ClassPathResolution",
    "ResolutionKind",
    "hint_for_step_violation",
    "repair_workflow_class_paths",
    "resolve_class_path",
]
