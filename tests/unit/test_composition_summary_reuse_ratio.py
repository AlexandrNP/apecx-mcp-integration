"""Unit tests for ``CompositionSummary.reuse_ratio`` + ``is_reuse_dominated``.

The reuse-first rule (2026-05-12) lives primarily in the LLM-facing
prompts, but the composer also tracks the underlying counts
(``steps_reused``, ``steps_generated``) — the derived ``reuse_ratio``
property surfaces this as a single number downstream consumers can
read (reviewer prompts, telemetry, quality gates).

Pins:
  1. ``reuse_ratio`` = reused / (reused + generated).
  2. Zero-step workflow returns 1.0 (vacuously library-compliant).
  3. ``steps_swapped`` is excluded from the ratio (it's a differ-mode
     count, not an authorship signal).
  4. ``is_reuse_dominated`` defaults to threshold 0.8.
  5. ``is_reuse_dominated`` accepts a caller-supplied threshold for
     stricter or looser policies.
  6. The property is a derived view — frozen-dataclass mutation is
     impossible, so callers cannot get a stale value.
"""

from __future__ import annotations

import dataclasses

import pytest

from apecx_integration.composition.composer_schemas import CompositionSummary


def _summary(*, reused: int, generated: int, swapped: int = 0) -> CompositionSummary:
    return CompositionSummary(
        steps_reused=reused,
        steps_generated=generated,
        steps_swapped=swapped,
        summary_sentence="test-fixture",
    )


def test_reuse_ratio_all_library() -> None:
    s = _summary(reused=5, generated=0)
    assert s.reuse_ratio == 1.0


def test_reuse_ratio_all_novel() -> None:
    s = _summary(reused=0, generated=3)
    assert s.reuse_ratio == 0.0


def test_reuse_ratio_mixed() -> None:
    s = _summary(reused=3, generated=1)
    assert s.reuse_ratio == 0.75


def test_reuse_ratio_zero_steps_is_one() -> None:
    """A vacuous workflow is not penalized — there's nothing novel
    to flag. Returning 1.0 keeps the threshold check defensible."""
    s = _summary(reused=0, generated=0)
    assert s.reuse_ratio == 1.0


def test_swapped_does_not_affect_reuse_ratio() -> None:
    """``steps_swapped`` is a differ-mode count (recompose substitutions),
    not an authorship signal — it must not enter the ratio."""
    s1 = _summary(reused=2, generated=2, swapped=0)
    s2 = _summary(reused=2, generated=2, swapped=10)
    assert s1.reuse_ratio == s2.reuse_ratio == 0.5


def test_is_reuse_dominated_default_threshold() -> None:
    """Default 0.8 — matches the composer's "median user's first
    workflow should have ZERO novel_python steps" adoption signal
    with one-step slack."""
    assert _summary(reused=4, generated=1).is_reuse_dominated() is True  # 0.8
    assert _summary(reused=3, generated=1).is_reuse_dominated() is False  # 0.75
    assert _summary(reused=5, generated=0).is_reuse_dominated() is True  # 1.0


def test_is_reuse_dominated_custom_threshold() -> None:
    """Strict 1.0 — only fully composed workflows pass."""
    s = _summary(reused=4, generated=1)
    assert s.is_reuse_dominated(threshold=0.8) is True
    assert s.is_reuse_dominated(threshold=0.9) is False
    assert s.is_reuse_dominated(threshold=1.0) is False


def test_summary_remains_frozen() -> None:
    """The frozen-dataclass invariant must hold — callers cannot set
    a stale value on ``reuse_ratio`` (it's a property, not a field)
    OR mutate the underlying counts."""
    s = _summary(reused=2, generated=2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.steps_reused = 999  # type: ignore[misc]
    # And reuse_ratio is read-only by virtue of being a property:
    with pytest.raises(AttributeError):
        s.reuse_ratio = 0.0  # type: ignore[misc]


def test_reuse_ratio_property_is_not_a_dataclass_field() -> None:
    """Adding ``reuse_ratio`` as a field would silently change the
    dataclass's __init__ signature. It's a property instead — verify
    that's still the case so a future "convert to field" refactor
    fails this test."""
    field_names = {f.name for f in dataclasses.fields(CompositionSummary)}
    assert "reuse_ratio" not in field_names, (
        "reuse_ratio became a dataclass field; it must remain a "
        "property so it cannot drift out of sync with steps_reused / "
        "steps_generated."
    )
