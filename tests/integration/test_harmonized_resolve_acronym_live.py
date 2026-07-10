"""Real-dictionary integration test for the acronym-expansion fallback in ``build_resolution_plan``.

Mock/integration parity for ``tests/unit/test_harmonized_resolve_step.py`` (which stubs
``lookup_entity``): here the SAME control flow runs against the REAL deployed synonym dictionary +
the REAL ``extract_virus_names`` alias table. Pins the production gap this change closes — a bare
virology acronym (LASV/MARV/NiV/RABV/DENV) must resolve to its canonical species IRI instead of
missing and serving the taxon-imprecise raw full-text fallback (measured precision 0.0).

Auto-skips when the deployed dictionary SQLite is absent (honest — no synthetic dict).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apecx_integration.composition.steps.harmonized_resolve_step import build_resolution_plan
from apecx_integration.synonym_dictionary.loader import configure_dictionary_path

pytestmark = pytest.mark.integration


def _deployed_dict() -> Path | None:
    env = os.environ.get("APECX_SYNONYM_DICT_PATH", "").strip()
    cand = (
        Path(env).expanduser()
        if env
        else Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"
    )
    return cand if cand.is_file() else None


needs_dict = pytest.mark.skipif(
    _deployed_dict() is None, reason="deployed synonym dictionary absent"
)

# (acronym, expected canonical NCBITaxon id) — verified live against the 2026-06-09 dict.
_ACRONYMS = [
    ("LASV", "NCBITaxon_3052310"),
    ("MARV", "NCBITaxon_3052505"),
    ("NiV", "NCBITaxon_3052225"),
    ("RABV", "NCBITaxon_11292"),
    ("DENV", "NCBITaxon_12637"),
]


@needs_dict
@pytest.mark.parametrize("acronym,expected_iri_suffix", _ACRONYMS)
def test_acronym_resolves_to_canonical_species(acronym, expected_iri_suffix):
    configure_dictionary_path(_deployed_dict())
    plan = build_resolution_plan(acronym, "bvbrc_genome", "pathogen")
    assert plan["resolution_path"] != "miss", (
        f"{acronym} still MISSES — acronym expansion not wired"
    )
    assert plan["canonical_iri"] is not None
    assert plan["canonical_iri"].endswith(expected_iri_suffix), (
        f"{acronym} → {plan['canonical_iri']} (expected …{expected_iri_suffix})"
    )


@needs_dict
def test_normal_name_still_resolves_unchanged():
    """A plain canonical name resolves on the first bare lookup — the fallback must not perturb it."""
    configure_dictionary_path(_deployed_dict())
    plan = build_resolution_plan("chikungunya virus", "bvbrc_genome", "pathogen")
    assert plan["resolution_path"] != "miss"
    assert plan["canonical_iri"].endswith("NCBITaxon_37124")
