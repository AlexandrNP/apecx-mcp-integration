"""Unit tests for surface-form normalization.

Both Stage 1 (build-time inverse index) and Stage 2 (runtime user-input
lookup) MUST produce identical normalizations — these tests pin the
exact behaviour so divergence becomes a test failure rather than a
silent breakage of the lookup path.
"""

from __future__ import annotations

import pytest
from apecx_integration.synonym_dictionary.normalization import (
    normalize_surface_form,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("EEEV", "eeev"),
        ("Chikungunya virus", "chikungunya virus"),
        ("  Yellow Fever Virus  ", "yellow fever virus"),
        ("Influenza A virus", "influenza a virus"),  # NBSP -> space
        ("FLU\tVIRUS", "flu virus"),  # tab collapse
        ("(Cancer)", "cancer"),  # surrounding parens
        ('"Influenza"', "influenza"),  # surrounding quotes
        ("", ""),
        ("ß", "ss"),  # casefold lowers German sharp-s
    ],
)
def test_normalize_known_cases(raw: str, expected: str) -> None:
    assert normalize_surface_form(raw) == expected


def test_normalize_idempotent() -> None:
    """``normalize(normalize(s)) == normalize(s)`` for any input."""
    samples = [
        "EEEV",
        "Chikungunya virus",
        "  Yellow Fever Virus  ",
        "(Cancer)",
        "Influenza A virus",
        "",
    ]
    for s in samples:
        once = normalize_surface_form(s)
        twice = normalize_surface_form(once)
        assert once == twice, f"non-idempotent for {s!r}: {once!r} -> {twice!r}"


def test_normalize_preserves_internal_punctuation() -> None:
    """Hyphens / dots / slashes are load-bearing in scientific labels."""
    assert normalize_surface_form("Influenza A/H1N1") == "influenza a/h1n1"
    assert normalize_surface_form("17D-204") == "17d-204"
    assert normalize_surface_form("VO_0000122") == "vo_0000122"
