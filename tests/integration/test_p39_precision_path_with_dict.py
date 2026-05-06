"""P3.9 precision-path integration test.

Verifies the full fast-path wiring:
  1. apecx-build-dictionary builds a real OLS dictionary from a VIOLIN row.
  2. The process singleton is configured with that dictionary.
  3. An MCP database tool call (query_pathogens) hits lookup_entity() on the
     fast path and uses the extracted NCBITaxon ID as a precision filter.
  4. The response carries a ``_resolution`` key showing path='fast' and the
     correct canonical IRI.

Gated on APECX_SYNONYM_DICT_LIVE_OLS=1 (Stage 1 calls real EBI OLS).

To run:

    APECX_SYNONYM_DICT_LIVE_OLS=1 \\
        PYTHONPATH=src .venv/bin/python -m pytest \\
        tests/integration/test_p39_precision_path_with_dict.py -v

Mock parity:
  tests/unit/test_database_tools.py — unit tests that patch lookup_entity()
  to return fast-path LookupResult objects and verify _resolution injection.
  Here we use a real OLS-built dictionary so lookup_entity() resolves
  through real synonyms + the real fast path.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_LIVE_OLS = os.environ.get("APECX_SYNONYM_DICT_LIVE_OLS", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE_OLS,
    reason=(
        "Set APECX_SYNONYM_DICT_LIVE_OLS=1 to run P3.9 precision-path "
        "integration tests that require a real dictionary artifact."
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
VIOLIN_PATHOGENS = WORKSPACE_ROOT / "data" / "violin" / "Pathogen_Information.csv"

# Eastern Equine Encephalitis Virus — row 50 in VIOLIN, NCBI taxon 11021.
# Confirmed present in the dataset by grep:
#   grep "Eastern Equine" data/violin/Pathogen_Information.csv → "11021.0"
EEEV_TAXON_ID = 11021
EEEV_IRI = f"http://purl.obolibrary.org/obo/NCBITaxon_{EEEV_TAXON_ID}"
EEEV_SEARCH_TERM = "eastern equine encephalitis"


@pytest.fixture(scope="module")
def eeev_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a Stage 1 dictionary from the first 60 VIOLIN pathogen rows.

    Row 50 is EEEV (NCBITaxon_11021) — the row we need for the precision test.
    Returns the path to the ``dictionary.sqlite`` artifact.
    """
    assert VIOLIN_PATHOGENS.exists(), (
        f"VIOLIN pathogen data not found at {VIOLIN_PATHOGENS}. "
        "Ensure the workspace data/ directory is populated."
    )

    from apecx_integration.synonym_dictionary.cli import main

    out = tmp_path_factory.mktemp("p39_dict")
    ret = main(
        [
            "--violin-pathogens",
            str(VIOLIN_PATHOGENS),
            "--output",
            str(out),
            "--dictionary-version",
            "test-p3.9",
            "--max-rows",
            "60",
            "--log-level",
            "WARNING",
        ]
    )
    assert ret == 0, f"apecx-build-dictionary exited with code {ret}"
    db_path = out / "dictionary.sqlite"
    assert db_path.exists(), f"dictionary.sqlite missing at {db_path}"
    return db_path


@pytest.fixture(scope="module", autouse=True)
def configure_singleton(eeev_dictionary: Path):
    """Wire the eeev_dictionary into the process singleton for this module."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import configure_dictionary_path

    _orig = _loader._singleton
    configure_dictionary_path(eeev_dictionary)
    yield
    _loader._singleton = _orig


# ---------------------------------------------------------------------------
# P3.9 fast path — query_pathogens with a real dictionary
# ---------------------------------------------------------------------------


def test_p39_query_pathogens_fast_path_injects_resolution():
    """query_pathogens must carry _resolution with path='fast' when dict loaded.

    Mock parity for:
    tests/unit/test_database_tools.py::test_query_pathogens_fast_path_injects_resolution
    """
    from apecx_integration.mcp_surface.tools import database_tools as tools

    out = asyncio.run(tools.query_pathogens(search_term=EEEV_SEARCH_TERM))
    assert "error" not in out, f"Tool returned error: {out}"
    assert "_resolution" in out, (
        "No _resolution key in output — lookup_entity() fast path did not fire "
        "or did not inject the _resolution metadata."
    )
    resolution = out["_resolution"]
    assert resolution["path"] == "fast", (
        f"Expected path='fast' for {EEEV_SEARCH_TERM!r}; got {resolution['path']!r}.  "
        "The dictionary may not contain EEEV (row 50 / NCBITaxon_11021)."
    )
    assert EEEV_IRI in resolution["canonical_iri"], (
        f"Expected EEEV IRI {EEEV_IRI!r} in canonical_iri; " f"got {resolution['canonical_iri']!r}"
    )
    assert resolution["confidence"] == 1.0


def test_p39_query_pathogens_precision_filter_narrows_results():
    """Fast path extracts NCBITaxon ID and passes it as ncbi_taxonomy_id precision filter.

    With the precision filter active, results should be limited to EEEV (taxon 11021)
    rather than the substring-match superset.  The precision result count ≤ substring count.
    """
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.mcp_surface.tools import database_tools as tools
    from apecx_integration.synonym_dictionary.loader import _ProcessSingleton

    # Precision path: dictionary loaded
    out_precision = asyncio.run(tools.query_pathogens(search_term=EEEV_SEARCH_TERM))
    assert "_resolution" in out_precision
    count_precision = out_precision["total_matching"]

    # Slow path: no dictionary
    _orig = _loader._singleton
    _loader._singleton = _ProcessSingleton()  # fresh, no path configured
    try:
        out_slow = asyncio.run(tools.query_pathogens(search_term=EEEV_SEARCH_TERM))
    finally:
        _loader._singleton = _orig

    assert "_resolution" not in out_slow, "Slow path should not inject _resolution"
    count_slow = out_slow["total_matching"]

    # Precision result count must be ≤ slow result count (precision is never
    # broader than substring).  They may be equal if there's only one EEEV row.
    assert count_precision <= count_slow, (
        f"Precision filter ({count_precision} rows) returned MORE results than "
        f"substring fallback ({count_slow} rows) — precision filter is broken."
    )
    # At least one result expected (EEEV is in the VIOLIN data)
    assert count_precision >= 1, "No EEEV rows found with precision filter"
