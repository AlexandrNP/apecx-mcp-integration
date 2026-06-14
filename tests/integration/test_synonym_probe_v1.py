"""SC-E1 — pin the 100-query synonym-dictionary probe set behavior.

Loads ``tests/integration/fixtures/synonym_probe_v1.jsonl`` and asserts
that each query resolves exactly as the fixture records. This is an
explicit regression contract: any change to the lookup pipeline that
moves a probe from (path=fast, iri=A) to anything else will trip a
failure, forcing the author to either revert OR re-run
``scripts/build_synonym_probe.py`` and explicitly re-pin the fixture.

Why pin on actual behavior rather than author belief? Because *silent
improvements are as dangerous as silent regressions* — when SC-B ships
and CHIKV starts resolving, we want the test to FAIL so the change
gets reviewed, the fixture gets re-baselined, and the SC-B implementation
log captures "moved 3 probes from miss to fast".

Gating: skipped unless the prod dictionary exists at the configured
path. The test is integration-grade — it exercises the real 281k-taxon
artifact, not a synthetic fixture.
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

_PROBE_PATH = Path(__file__).parent / "fixtures" / "synonym_probe_v1.jsonl"
_DEFAULT_DICT = Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"


def _resolve_dict_path() -> Path:
    env = os.environ.get("APECX_SYNONYM_DICT_PATH")
    return Path(env) if env else _DEFAULT_DICT


def _load_probes() -> list[dict]:
    if not _PROBE_PATH.exists():
        return []
    rows = []
    with _PROBE_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


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


# Parametrize on the probe rows so the test report itemizes each probe
# by query — a single failure stays diagnosable without printing the
# full set.
_PROBES = _load_probes()


@pytest.mark.skipif(
    not _PROBE_PATH.exists(),
    reason=f"probe fixture missing: {_PROBE_PATH}",
)
@pytest.mark.parametrize(
    "probe",
    _PROBES,
    ids=[f"{p['scenario']}:{p['query'][:40]}" for p in _PROBES],
)
def test_probe_pins_actual_behavior(probe: dict) -> None:
    """Each probe row must resolve EXACTLY as the JSONL records.

    Any deviation = the fixture is out of date with the code. Re-run
    ``scripts/build_synonym_probe.py`` after deciding whether the
    behavior change is intentional.
    """
    result = lookup_entity(probe["query"], entity_type=EntityType.PATHOGEN)

    assert result.path == probe["actual_path"], (
        f"path drift: probe {probe['query']!r} "
        f"was pinned to path={probe['actual_path']!r} but now resolves "
        f"to {result.path!r}. Re-baseline via build_synonym_probe.py."
    )
    assert result.canonical_iri == probe["actual_iri"], (
        f"iri drift: probe {probe['query']!r} "
        f"was pinned to iri={probe['actual_iri']!r} but now resolves "
        f"to {result.canonical_iri!r}."
    )
    assert result.resolution_status.value == probe["actual_resolution_status"], (
        f"status drift on {probe['query']!r}"
    )
    assert len(result.candidates) == probe["actual_candidate_count"], (
        f"candidate count drift on {probe['query']!r}: was "
        f"{probe['actual_candidate_count']}, now {len(result.candidates)}"
    )


def test_probe_set_size_and_balance() -> None:
    """The probe set must carry ≥20 entries per SC-E1 scenario category.

    Expanded 2026-06-08 (SC-E5b): +50 biology-adjacent adversarial-noise
    probes added to the ``unresolvable`` category. Original SC-E1 minimum
    was 100; current floor is 150 to lock in the SC-E5b expansion.
    """
    assert len(_PROBES) >= 150, (
        f"expected ≥150 probes (100 SC-E1 + 50 SC-E5b adversarial), found {len(_PROBES)}"
    )
    from collections import Counter

    counts = Counter(p["scenario"] for p in _PROBES)
    for scenario in (
        "scientific_name",
        "acronym",
        "common_name",
        "typo",
        "unresolvable",
    ):
        assert counts[scenario] >= 20, (
            f"SC-E1 calls for ≥20 of each scenario; {scenario} has {counts[scenario]}"
        )
    # SC-E5b: ≥50 adversarial_noise probes added as a separate scenario.
    assert counts["adversarial_noise"] >= 50, (
        f"SC-E5b calls for ≥50 adversarial_noise probes; found {counts['adversarial_noise']}"
    )


def test_unresolvable_probes_actually_miss() -> None:
    """Defensive check: no probe in the 'unresolvable' (pure-noise)
    category should EVER resolve. If one does, the dictionary has
    acquired a false positive on pure noise — which would surface in
    production as a user typing garbage and getting a confident
    (wrong) lookup.

    The companion ``adversarial_noise`` scenario (SC-E5b) is biology-
    adjacent and intentionally not subject to this strict invariant:
    some adversarial probes (Coronaviridae, retrovirus) legitimately
    resolve to real NCBI taxa. See SC-E5b calibration for the FPR
    measurement on that population.
    """
    for probe in _PROBES:
        if probe["scenario"] != "unresolvable":
            continue
        assert probe["actual_path"] == "miss", (
            f"PRECISION REGRESSION: unresolvable probe "
            f"{probe['query']!r} now resolves to "
            f"{probe['actual_path']!r} — false positive on pure noise. "
            f"Fix the lookup or tighten the fuzzy threshold."
        )


def test_adversarial_noise_fpr_bounded() -> None:
    """SC-E5b — at the current fuzzy floor (0.70) the false-positive
    rate on biology-adjacent adversarial noise must stay below 30%.

    The calibration measured 15.3% at the 2026-06-08 ship. The 30% guard
    rail catches a future change that would dramatically loosen the
    fuzzy floor / band gate / near-tie window. Tighter than 30% would
    risk false failures on legitimate adversarial probes that share
    real taxonomy names (Coronaviridae, retrovirus, etc.).
    """
    adversarial = [p for p in _PROBES if p["scenario"] == "adversarial_noise"]
    if not adversarial:
        pytest.skip("no adversarial probes in fixture")
    lifted = sum(1 for p in adversarial if p["actual_path"] != "miss")
    fpr = lifted / len(adversarial)
    assert fpr <= 0.30, (
        f"adversarial-noise FPR {fpr:.3f} exceeds 30% guard rail "
        f"({lifted}/{len(adversarial)} biology-adjacent probes lifted). "
        f"Re-run scripts/calibrate_fuzzy_threshold.py and review."
    )
