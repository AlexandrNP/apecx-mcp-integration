"""Consumer disambiguation overrides — the server dict flags more surface forms
ambiguous (old/new ICTV name pairs of one organism, ambiguous acronyms); these
overrides restore a single canonical resolution for a curated set. The integration
tests run against the real published dict (no mocks); set APECX_SYNONYM_DICT_PATH.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apecx_integration.synonym_dictionary.disambiguation_overrides import (
    DISAMBIGUATION_OVERRIDES,
)
from apecx_integration.synonym_dictionary.normalization import normalize_surface_form

_OBO = "http://purl.obolibrary.org/obo/NCBITaxon_"
_DICT = os.environ.get("APECX_SYNONYM_DICT_PATH")
_have_dict = bool(_DICT) and Path(_DICT).exists()
_skip = pytest.mark.skipif(
    not _have_dict, reason="needs the published dict (set APECX_SYNONYM_DICT_PATH)"
)


def test_override_constant_is_wellformed():
    """Every value is an NCBITaxon IRI and every key is already in normalized form
    (so the runtime ``normalize_surface_form(input)`` lookup can hit it)."""
    assert DISAMBIGUATION_OVERRIDES, "override map must not be empty"
    for surface, iri in DISAMBIGUATION_OVERRIDES.items():
        assert iri.startswith(_OBO), f"{surface!r}: not an NCBITaxon IRI: {iri}"
        assert surface == normalize_surface_form(surface), f"{surface!r}: key not normalized"


@_skip
def test_overrides_resolve_via_override_path():
    """An overridden surface form resolves cleanly with the VISIBLE ``override``
    path (not disguised as a dict hit) to the curated taxon, and normalization
    means a mixed-case input hits the same override."""
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    r = lookup_entity("sars-cov")
    assert r.path == "override"
    assert r.canonical_iri == DISAMBIGUATION_OVERRIDES["sars-cov"]
    assert r.resolution_status.value == "id_anchored"
    assert lookup_entity("SARS-CoV").path == "override"  # normalization
    # every overridden surface form resolves (none point at a missing entry)
    for surface in DISAMBIGUATION_OVERRIDES:
        res = lookup_entity(surface)
        assert res.path == "override", f"{surface!r} did not take the override path"
        assert res.canonical_iri == DISAMBIGUATION_OVERRIDES[surface]


@_skip
def test_override_does_not_shadow_clean_resolution():
    """A non-overridden term resolves via its normal path — the overlay is scoped."""
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    assert lookup_entity("influenza A virus").path == "fast"
    assert lookup_entity("influenza A virus").path != "override"
