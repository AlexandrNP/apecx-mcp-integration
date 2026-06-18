"""Shared BV-BRC exact-CDS coverage probe for the taxon-resolution fallback steps.

Both ``BvbrcTaxonomySearchStep`` (rank candidates by coverage) and ``TaxonCandidateReviewStep``
(re-verify the winner) need the exact ``genome_feature`` CDS count for a taxon. The count is the
fetchable-coverage signal the conservation leg's exact ``eq(taxon_id,X)&CDS`` fetch depends on —
a genus can have many genomes yet ~0 CDS filed at its exact taxon_id. Kept here once so the two
steps cannot drift.
"""

from __future__ import annotations

import re

import requests


def content_range_total(header: str | None) -> int:
    """Parse the BV-BRC ``Content-Range: items a-b/N`` header, returning ``N`` (0 if absent)."""
    if not header:
        return 0
    m = re.search(r"/\s*(\d+)\s*$", header)
    return int(m.group(1)) if m else 0


def cds_count(api_base: str, taxon_id: int, timeout: float) -> int:
    """Total ``genome_feature`` CDS rows for a taxon (exact taxon_id match), from the
    Content-Range header — the fetchable-coverage signal for the conservation leg."""
    query = f"eq(taxon_id,{taxon_id})&eq(feature_type,CDS)&limit(1)"
    url = f"{api_base}/genome_feature/?{query}&http_accept=application/json"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return content_range_total(resp.headers.get("Content-Range"))
