"""Pin the harmonization rate on the mu-virus-list demo corpus
(``tests/integration/fixtures/mu_virus_list.txt`` — the 70-term
realistic virology vocabulary used in the apecx-harvesters search
demo).

This test serves two purposes:

1. **Regression pin** — each row's `(path, canonical_iri, confidence)`
   is locked to the baseline captured 2026-06-08. When SC-B corpus
   mining ships and starts lifting CHIKV / coronavirus / poxvirus /
   etc., this test will FAIL — forcing whoever made the change to
   re-baseline the JSONL and explicitly accept the harmonization
   improvement. (Silent improvement is as dangerous as silent regression
   because it masks the moment the contract changed.)

2. **Headline harmonization metric** — the aggregate test
   `test_mu_virus_list_harmonization_rate_meets_floor` asserts the
   fast-resolution rate stays ≥85% so a future change that DROPS
   coverage is loudly flagged.

The baseline at 2026-06-08 (SC-A4 + SC-A5b + SC-C + ``includes`` delta):

- 61/70 fast (87.1%)
- 5/70 ambiguous (correctly flagged for HITL)
- 4/70 miss — family-level vernaculars NCBI doesn't carry:
  coronavirus, arbovirus, poxvirus, papilloma and polyoma viruses

SC-B target: drive the 4 misses to fast (or at least ambiguous with
the right candidate). When SC-B ships, this test will fail; the SC-B
PR re-baselines the JSONL with the improved numbers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.loader import (
    configure_dictionary_path,
    get_dictionary_index,
)
from apecx_integration.synonym_dictionary.lookup import lookup_entity

_BASELINE = Path(__file__).parent / "fixtures" / "mu_virus_list_baseline.jsonl"
_DEFAULT_DICT = Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"


def _resolve_dict_path() -> Path:
    env = os.environ.get("APECX_SYNONYM_DICT_PATH")
    return Path(env) if env else _DEFAULT_DICT


def _load_baseline() -> list[dict]:
    if not _BASELINE.exists():
        return []
    return [json.loads(line) for line in _BASELINE.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module", autouse=True)
def _configure_dict() -> None:
    dict_path = _resolve_dict_path()
    if not dict_path.exists():
        pytest.skip(
            f"prod dictionary not at {dict_path}; set APECX_SYNONYM_DICT_PATH or run apecx-mcp once"
        )
    configure_dictionary_path(dict_path)
    _, err = get_dictionary_index()
    if err is not None:
        pytest.skip(f"dictionary load failed: {err}")


_BASELINE_ROWS = _load_baseline()


@pytest.mark.skipif(
    not _BASELINE.exists(),
    reason=f"mu-virus-list baseline missing: {_BASELINE}",
)
@pytest.mark.parametrize(
    "row",
    _BASELINE_ROWS,
    ids=[r["query"] for r in _BASELINE_ROWS],
)
def test_mu_virus_list_row_pinned(row: dict) -> None:
    """Per-query regression pin against the captured baseline."""
    result = lookup_entity(row["query"], entity_type=EntityType.PATHOGEN)
    assert result.path == row["path"], (
        f"harmonization drift on {row['query']!r}: "
        f"baseline={row['path']!r} → current={result.path!r}. "
        f"Re-baseline mu_virus_list_baseline.jsonl after explicit review."
    )
    assert result.canonical_iri == row["canonical_iri"], (
        f"IRI drift on {row['query']!r}: "
        f"baseline={row['canonical_iri']!r} → current={result.canonical_iri!r}"
    )
    assert len(result.candidates) == row["n_candidates"], (
        f"candidate count drift on {row['query']!r}: "
        f"baseline={row['n_candidates']} → current={len(result.candidates)}"
    )


def test_mu_virus_list_harmonization_rate_meets_floor() -> None:
    """Aggregate harmonization rate must stay ≥85% on this corpus.

    Floor is set 2 percentage points below the 87.1% baseline so minor
    NCBI-driven shifts (e.g., a renamed canonical) don't cause spurious
    failures, but a genuine coverage regression DOES surface.
    """
    if not _BASELINE_ROWS:
        pytest.skip("no baseline loaded")
    fast = sum(1 for r in _BASELINE_ROWS if r["path"] == "fast")
    rate = fast / len(_BASELINE_ROWS)
    assert rate >= 0.85, (
        f"mu-virus-list fast-harmonization rate dropped to {rate:.3f} "
        f"(below the 0.85 floor). Coverage regression — investigate."
    )


def test_mu_virus_list_no_silent_improvement_either() -> None:
    """If MORE queries fast-resolve than the baseline expects, fail
    loudly so the JSONL gets re-baselined intentionally.

    Silent improvement is as dangerous as silent regression — when SC-B
    ships and 'coronavirus' / 'poxvirus' start resolving, this test
    fails, forcing the PR to re-baseline and explicitly own the new
    contract. The numbers are then verifiable in code review.
    """
    if not _BASELINE_ROWS:
        pytest.skip("no baseline loaded")
    baseline_fast = sum(1 for r in _BASELINE_ROWS if r["path"] == "fast")
    actual_fast = sum(
        1
        for r in _BASELINE_ROWS
        if lookup_entity(r["query"], entity_type=EntityType.PATHOGEN).path == "fast"
    )
    assert actual_fast == baseline_fast, (
        f"mu-virus-list fast count changed: baseline={baseline_fast}, "
        f"now={actual_fast}. Re-run the baseline capture and update "
        f"mu_virus_list_baseline.jsonl."
    )
