"""Gated real-data test of the per-clade broad-effectiveness path on a DIVERGENT virus.

The unit tests (tests/unit/test_clade_breadth.py) prove the clustering + pan-clade/clade-restricted
logic on synthetic data. This one runs it on a genuinely divergent real protein — dengue (species
taxon 12637) envelope E across serotypes — so the MULTI-CLADE path (not the homogeneous degrade)
executes against real BV-BRC sequences + real MAFFT. CHIKV E1, the canonical epitope e2e, is
homogeneous (1 clade), so it does NOT exercise this path — verifying a clade-divergence feature
only on a homogeneous protein is the silent-failure anti-pattern this test exists to prevent.

Gated (network + MAFFT, ~30-60s): set ``APECX_CLADE_LIVE=1`` to run.

Recorded baseline (2026-06-17): dengue E taxon 12637 → 16 aligned sequences → 2 clades (9, 6) +
1 ungrouped outlier at 0.95 identity → 42 pan-clade + 21 clade-restricted regions (e.g. a
clade-restricted region where clade A consensus 'RRTF' vs clade B 'KHSM').
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("APECX_CLADE_LIVE") != "1",
    reason="set APECX_CLADE_LIVE=1 to run the live dengue clade-divergence path (network + MAFFT)",
)


def test_dengue_multi_clade_breadth_against_real_data():
    from apecx_integration.composition.steps._clade_grouping import (
        clade_conservation_breadth,
        cluster_by_identity,
    )
    from apecx_integration.composition.steps.conservation_score_step import _parse_fasta
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        build_viral_conserved_sites_core_workflow,
    )

    os.environ["APECX_CONSERVED_SITES_NOCACHE"] = "1"  # force a fresh MAFFT run

    async def _run():
        wf = build_viral_conserved_sites_core_workflow(aligner="mafft")
        return await wf.run({"workflow_input": {"taxon_id": 12637, "protein": "E"}})

    out = asyncio.run(_run())
    cons = ((out.get("workflow_output") or {}).get("data") or {}).get("parts") or {}
    alignment_fasta = cons.get("alignment_fasta") or ""
    assert alignment_fasta, "dengue E fetch/align produced no alignment"

    aligned = _parse_fasta(alignment_fasta)
    clustering = cluster_by_identity(aligned, threshold=0.95, min_size=2)
    # Dengue E across serotypes MUST split into >=2 clades at 0.95 (the whole point of the test).
    assert len(clustering["clades"]) >= 2, f"expected divergence; got {clustering}"

    breadth = clade_conservation_breadth(
        aligned, clustering["clades"], identity_threshold=0.9, min_region=3
    )
    assert breadth["available"]
    # A genuinely divergent set yields BOTH broad-spectrum and clade-restricted regions.
    assert breadth["pan_clade_regions"], "expected some pan-clade (broad-spectrum) regions"
    assert breadth["clade_restricted_regions"], "expected some clade-restricted regions"
    # A clade-restricted region records a DIFFERING per-clade consensus (the divergence signal).
    restr = breadth["clade_restricted_regions"][0]
    assert len(set(restr["per_clade_consensus"])) > 1
