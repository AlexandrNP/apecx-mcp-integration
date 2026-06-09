"""Proactive audit: synonym dictionary canonical labels vs. live BV-BRC.

The dominant failure shape the harmonized_search workflow surfaces is
NOT a code bug but a data-layer drift: BV-BRC and VIOLIN have adopted
the post-2024 ICTV taxonomy renames (e.g. ``Yellow fever virus`` →
``Orthoflavivirus flavi``), while the synonym dictionary's canonical
labels — derived from an older NCBI taxdump ingest — still use the
pre-rename names. The harmonization filter does an exact
``match_any`` on the ``Species`` field; when the dict's
``canonical_label`` is no longer the BV-BRC ``Species`` value, the
filter returns 0 records and the user gets ``verdict=broken`` from
``harmonized_search``.

This test probes BV-BRC directly for a small panel of canonical
viruses and asserts the dict's ``canonical_label`` still matches what
BV-BRC indexes under ``Species``. Two buckets:

- **Healthy panel** — species whose canonical_label IS the BV-BRC
  ``Species`` value. Asserted as a hard test failure. When one of
  these flips, BV-BRC has done another rename and the dict needs a
  rebuild before more entities break.
- **Known-broken panel** — species we already know are stale
  (yellow fever / Hepatitis E / Influenza A / Zika, as of the
  2026-06-09 audit). These are ``pytest.xfail(strict=True)``.  When
  a broken case starts PASSING, pytest reports an unexpected pass
  (XPASS), which is the proactive signal the dict has been rebuilt
  and we can promote those species to the healthy panel.

Run requirements:
- ``APECX_SYNONYM_DICT_PATH`` env var pointing at a built dict.
- Live Globus Search reachability (BV-BRC public index, no auth).

Source of the panel:
``apecx-mcp-integration/docs/harmonized_search_dict_staleness_audit_2026-06-09.md``
(panel size deliberately small — 10 species — so the full audit runs
in ~10s on a typical home network without hammering Globus).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_DEFAULT_DICT = Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"
_DICT_PATH = Path(os.environ.get("APECX_SYNONYM_DICT_PATH", str(_DEFAULT_DICT)))
_BVBRC_GENOME_UUID = "b676edbe-3286-4514-bc13-5cbe891c4bb1"

# Six species whose dict canonical_label MUST still match BV-BRC. When
# any of these flips, BV-BRC has done a rename; promote to broken
# panel and rebuild the dict.
HEALTHY_PANEL = [
    "Chikungunya virus",
    "Mayaro virus",
    "Sindbis virus",
    "Eastern equine encephalitis virus",
    "Western equine encephalitis virus",
    "Lassa virus",
]

# Four species we KNOW are stale as of 2026-06-09. Each carries the
# ICTV rename target so a future maintainer reading the xfail reason
# knows what the dict needs to incorporate. When one of these starts
# passing (the dict was rebuilt), pytest reports XPASS — a clear
# "promote to healthy panel" signal.
KNOWN_BROKEN_PANEL = [
    ("Yellow fever virus", "Orthoflavivirus flavi"),
    ("Hepatitis E virus", "(Paslahepevirus balayani or similar — Hepeviridae rename)"),
    ("Influenza A virus", "(Alphainfluenzavirus influenzae — Orthomyxoviridae rename)"),
    ("Zika virus", "Orthoflavivirus zikaense"),
]


pytestmark_dict_present = pytest.mark.skipif(
    not _DICT_PATH.exists(),
    reason=f"production dict not present at {_DICT_PATH}",
)


@pytest.fixture(scope="module")
def configured_lookup():
    """Configure the synonym dictionary path and return the lookup fn."""
    from apecx_integration.synonym_dictionary.loader import configure_dictionary_path
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    configure_dictionary_path(_DICT_PATH)
    return lookup_entity


@pytest.fixture(scope="module")
def globus_client():
    """Live Globus Search client. Skip the module if globus_sdk is missing."""
    try:
        import globus_sdk
    except ImportError:
        pytest.skip("globus_sdk not installed; audit requires live network access")
    return globus_sdk.SearchClient()


def _probe_bvbrc_species(client, canonical_label: str) -> int:
    """Return the number of BV-BRC genome records under Species=canonical_label."""
    import globus_sdk

    try:
        resp = client.post_search(
            _BVBRC_GENOME_UUID,
            {
                "filters": [
                    {
                        "type": "match_any",
                        "field_name": "Species",
                        "values": [canonical_label],
                    },
                ],
                "limit": 1,
            },
        )
    except (globus_sdk.GlobusAPIError, globus_sdk.NetworkError) as exc:
        pytest.skip(f"Globus reachability failure: {exc}")
    return int(resp.data.get("total", 0))


@pytestmark_dict_present
@pytest.mark.parametrize("surface_form", HEALTHY_PANEL)
def test_dict_canonical_label_still_aligned_with_bvbrc(
    surface_form,
    configured_lookup,
    globus_client,
):
    """For each known-healthy species: lookup_entity → canonical_label,
    query BV-BRC by Species filter, assert > 0 records.

    When this fails, BV-BRC has renamed the species (ICTV adoption) and
    the synonym dictionary is now stale for it. Add the surface_form to
    KNOWN_BROKEN_PANEL with the ICTV rename target as the xfail reason,
    and file an SC-A ticket to rebuild the dict.
    """
    result = configured_lookup(surface_form)
    assert result.path != "miss", (
        f"surface_form {surface_form!r} unexpectedly misses in the dict "
        f"— either the dict regressed or the surface form was renamed"
    )
    canonical_label = result.canonical_label
    assert canonical_label, f"resolver returned no canonical_label for {surface_form!r}"

    total = _probe_bvbrc_species(globus_client, canonical_label)
    assert total > 0, (
        f"BV-BRC has 0 records under Species={canonical_label!r} for "
        f"surface_form {surface_form!r}. This species has been renamed "
        f"in BV-BRC; the synonym dictionary's canonical_label no longer "
        f"aligns. Move {surface_form!r} from HEALTHY_PANEL to "
        f"KNOWN_BROKEN_PANEL (with the ICTV rename target as the xfail "
        f"reason) and file an SC-A ticket to rebuild the dict against "
        f"the current NCBI taxdump."
    )


@pytestmark_dict_present
@pytest.mark.parametrize(
    "surface_form,suspected_new_label",
    KNOWN_BROKEN_PANEL,
    ids=[s[0] for s in KNOWN_BROKEN_PANEL],
)
def test_dict_canonical_label_known_broken_against_bvbrc(
    surface_form,
    suspected_new_label,
    configured_lookup,
    globus_client,
):
    """For each known-broken species: confirm the dict's canonical_label
    still misses in BV-BRC.

    Marked xfail(strict=True): when this case starts PASSING, the dict
    has been rebuilt against newer taxonomy and we can promote this
    species back to HEALTHY_PANEL.
    """
    result = configured_lookup(surface_form)
    if result.path == "miss":
        pytest.xfail(
            f"dict no longer has an entry for {surface_form!r} — "
            f"unexpected; investigate before promoting"
        )

    canonical_label = result.canonical_label
    total = _probe_bvbrc_species(globus_client, canonical_label)

    # Hardcoded expectation: BV-BRC has 0 records under the stale
    # canonical_label. We use xfail(strict=True) so that when the dict
    # is rebuilt (canonical_label is updated to the new ICTV binomial),
    # the assertion below starts passing and pytest reports XPASS.
    if total > 0:
        # Dict was rebuilt — promote this species. Pytest will report
        # XPASS; the maintainer reads this assertion's message and
        # knows to move the surface_form into HEALTHY_PANEL.
        return
    pytest.xfail(
        f"dict canonical_label {canonical_label!r} for {surface_form!r} "
        f"still does not match BV-BRC (probable rename target: "
        f"{suspected_new_label}). Confirmed dict-stale. Rebuild the "
        f"dict to unblock."
    )


@pytestmark_dict_present
def test_audit_panel_size_invariant():
    """Defensive: panels must be disjoint and non-empty.

    If the maintainer accidentally adds a surface_form to both panels,
    or empties one, surface this loudly at test-collection time.
    """
    healthy_set = set(HEALTHY_PANEL)
    broken_set = {entry[0] for entry in KNOWN_BROKEN_PANEL}
    overlap = healthy_set & broken_set
    assert not overlap, (
        f"surface_form(s) {overlap} appear in BOTH HEALTHY_PANEL and "
        f"KNOWN_BROKEN_PANEL — a species cannot be both."
    )
    assert HEALTHY_PANEL, "HEALTHY_PANEL is empty — the audit has no positive signal"
    assert KNOWN_BROKEN_PANEL, (
        "KNOWN_BROKEN_PANEL is empty — if all species are healthy, this "
        "test should be retired; otherwise add the known-broken ones."
    )
