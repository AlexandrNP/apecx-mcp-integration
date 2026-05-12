"""Pin the supervisor handbook's structural sections.

The handbook (``docs/supervisor_handbook.md``) is a knowledge-transfer
artifact for the next external supervisor of the apecx composer.
A future "let me clean up the docs" edit must not silently drop
its load-bearing sections — each section maps to a real operational
need (drift patterns, gates, signals, distillation policy).

Pins:
  1. The marker phrase ``SUPERVISOR HANDBOOK`` appears at least once
     so reverse-lookup grep works.
  2. Each required structural section is present (one row per
     section heading the handbook must carry).
  3. Each drift pattern (D1-D8) is present (one row per pattern).
  4. The cross-references to existing docs / skills are present
     so the handbook stays linked to the rest of the repo.

If you intentionally remove a section, delete the corresponding row
from REQUIRED_SECTIONS or REQUIRED_DRIFT_PATTERNS in the same PR and
document the reason. Silent removal is what this test prevents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDBOOK_PATH = REPO_ROOT / "docs" / "supervisor_handbook.md"

MARKER = "SUPERVISOR HANDBOOK"

REQUIRED_SECTIONS = (
    "## Scope — what supervision IS and IS NOT",
    "## Day-one checklist",
    "## Drift patterns observed",
    "## Gates and rules currently shipped",
    "## Signals to monitor",
    "## When to stop and ask",
    "## Session-end distillation",
    "## Anti-patterns",
    "## Cross-references",
    "## Honest unfiltered notes from the prior supervisor",
)

REQUIRED_DRIFT_PATTERNS = (
    "D1 — Prose prefix on code output",
    "D2 — Code fences in the output",
    "D3 — Wrong function name",
    "D4 — Vacuous bug-fix",
    "D5 — Trigger-binding gap",
    "D6 — Nested-cascade hang",
    "D7 — Composer hallucinates inline",
    "D8 — Composer hallucinates class-path suffixes",
)

REQUIRED_CROSS_REFS = (
    "CLAUDE.md",
    "docs/architecture.md",
    "docs/_design_index.md",
    "docs/CONTRACTS.md",
    "docs/implementation_task_graph.md",
    ".claude/skills/nanobrain-",
    "memory/code_writing/CITATIONS.md",
    "session_friction_log.md",
)


def _read_handbook() -> str:
    assert HANDBOOK_PATH.exists(), (
        f"supervisor handbook missing at {HANDBOOK_PATH.relative_to(REPO_ROOT)}; "
        f"it is the knowledge-transfer artifact for new external "
        f"supervisors of the apecx composer. Restore it from git "
        f"history (commit ec1477a+ era) before continuing."
    )
    return HANDBOOK_PATH.read_text(encoding="utf-8")


def test_marker_is_present():
    """The marker phrase MUST appear so reverse-lookup grep works."""
    body = _read_handbook()
    assert MARKER in body, (
        f"{HANDBOOK_PATH.relative_to(REPO_ROOT)} no longer contains "
        f"the {MARKER!r} marker phrase. Restore it near the top of "
        f"the document — it's the anchor that pinning tests and "
        f"future skills grep against."
    )


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_required_section_is_present(section: str):
    """Each section in REQUIRED_SECTIONS maps to a real operational
    need; silent removal hurts a new supervisor's onboarding."""
    body = _read_handbook()
    assert section in body, (
        f"{HANDBOOK_PATH.relative_to(REPO_ROOT)} is missing the "
        f"required section {section!r}. If this removal is "
        f"intentional, delete the row from REQUIRED_SECTIONS in "
        f"this test and document the reason in the same PR."
    )


@pytest.mark.parametrize("pattern", REQUIRED_DRIFT_PATTERNS)
def test_required_drift_pattern_is_present(pattern: str):
    """Each D-numbered pattern was observed in real supervision
    sessions. Removing one without replacement loses operational
    knowledge for the next supervisor."""
    body = _read_handbook()
    assert pattern in body, (
        f"{HANDBOOK_PATH.relative_to(REPO_ROOT)} is missing drift "
        f"pattern {pattern!r}. If a pattern was retired (e.g., a "
        f"gate eliminated it), keep the row but mark it RETIRED — "
        f"do not delete the row, or future sessions will lose the "
        f"detection signal."
    )


@pytest.mark.parametrize("xref", REQUIRED_CROSS_REFS)
def test_required_cross_reference_is_present(xref: str):
    """Cross-references keep the handbook tied to the rest of the
    repo's source-of-truth surface. A handbook without these links
    becomes a stale satellite document."""
    body = _read_handbook()
    assert xref in body, (
        f"{HANDBOOK_PATH.relative_to(REPO_ROOT)} no longer references "
        f"{xref!r}. Either restore the link OR (if the referenced "
        f"resource was moved/renamed) update REQUIRED_CROSS_REFS in "
        f"this test in the same PR."
    )


def test_handbook_is_scannable_size():
    """Soft cap: under 25 KB. The handbook is meant to be scannable,
    not a comprehensive manual. Past 25 KB the next supervisor will
    skim instead of read, defeating the artifact's purpose.

    If you exceed this cap, the right answer is usually to split the
    handbook into the handbook (scannable) + an appendix (longform),
    not to lift the cap."""
    size = HANDBOOK_PATH.stat().st_size
    assert size <= 25 * 1024, (
        f"supervisor handbook is {size} bytes "
        f"({size / 1024:.2f} KB), exceeding the 25 KB scannability "
        f"cap. Consider splitting into handbook + appendix instead "
        f"of lifting the cap."
    )
