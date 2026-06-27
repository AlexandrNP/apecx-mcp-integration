"""EO-54b — pluggable-aligner substitution: MAFFT (local) ↔ MUSCLE (Rhea) parity.

The conserved-sites workflow offers two interchangeable aligner backends at its ``align`` step,
selected by the catalog entry: ``viral_conserved_sites`` (local MAFFT) and
``viral_conserved_sites_muscle`` (MUSCLE dispatched over the Rhea MCP server). The rest of the
pipeline is aligner-agnostic because both align steps emit the SAME
``{"alignment": {alignment_fasta, n_sequences, alignment_length, aligner, ...}}`` shape.

Two test surfaces:

  1. **Unconditional** — both no-arg builders construct; the ``align`` step is the right class
     for each; the muscle catalog entry honestly reports UNAVAILABLE when Rhea is absent while
     the mafft entry stays available. No external services.

  2. **Gated on MAFFT + BV-BRC + $RHEA_MCP_URL** — the substitution AC on REAL CHIKV data: the
     SAME query through both aligners yields conserved sites, and the conserved-region counts
     AGREE within tolerance. Crucially it ALSO asserts the two alignments are NOT byte-identical
     — the silent-failure guard proving the muscle path genuinely dispatched to MUSCLE over Rhea
     rather than shortcutting to the mafft result (which would make the count-parity assertion
     pass while the feature is broken).

The gated test auto-skips when its dependencies are absent, so this file is CI-safe.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil

import pytest
import requests

pytestmark = pytest.mark.integration

_CHIKV_TAXON = 37124
_QUERY = {"taxon_id": _CHIKV_TAXON, "protein": "structural polyprotein"}


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            "https://www.bv-brc.org/api/genome_feature/"
            f"?eq(taxon_id,{_CHIKV_TAXON})&limit(1)&http_accept=application/json",
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


def _rhea_reachable() -> bool:
    url = os.environ.get("RHEA_MCP_URL")
    if not url:
        return False
    try:
        # Any HTTP response (even 406 for a bare GET) means the server is listening.
        requests.get(url, timeout=5)
        return True
    except Exception:
        return False


needs_both_aligners = pytest.mark.skipif(
    shutil.which("docker") is None or not _bvbrc_reachable() or not _rhea_reachable(),
    reason="needs MAFFT + BV-BRC reachable + a live Rhea MCP server ($RHEA_MCP_URL)",
)


def _region_count(markdown: str) -> int | None:
    """Pull the conserved-region count out of the conservation report markdown."""
    m = re.search(r"across \*\*(\d+)\*\* region", markdown or "")
    return int(m.group(1)) if m else None


def _conserved_columns(markdown: str) -> int | None:
    m = re.search(r"Found \*\*(\d+)\*\* conserved column", markdown or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Unconditional — construction + honest availability
# ---------------------------------------------------------------------------


def test_both_builders_construct_with_the_right_align_step():
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        build_viral_conserved_sites_muscle_workflow,
        build_viral_conserved_sites_workflow,
    )

    def align_cls(wf) -> str:
        children = (
            getattr(wf, "child_steps", None)
            or getattr(wf, "_child_steps", None)
            or getattr(wf, "steps", None)
            or {}
        )
        return type(children["align"]).__name__

    assert align_cls(build_viral_conserved_sites_workflow()) == "LocalMafftAlignStep"
    assert align_cls(build_viral_conserved_sites_muscle_workflow()) == "RheaMuscleAlignStep"


def test_unknown_aligner_fails_loud():
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        build_viral_conserved_sites_workflow,
    )

    with pytest.raises(ValueError, match="unknown aligner"):
        build_viral_conserved_sites_workflow(aligner="clustalw")


def test_muscle_entry_unavailable_without_rhea(monkeypatch):
    # With no Rhea, list_workflows must report the muscle variant UNAVAILABLE (its `requires`
    # declares RHEA_MCP_URL + the rhea module) while the mafft variant stays available — honest
    # per-entry availability, not a single workflow that silently fails on aligner=muscle.
    monkeypatch.delenv("RHEA_MCP_URL", raising=False)
    from apecx_integration.mcp_surface.tools.discovery import list_workflows

    res = asyncio.run(list_workflows())
    by_name = {w.get("name"): w for w in res.get("runnable", [])}
    assert by_name["viral_conserved_sites"]["available"] is True
    # muscle is unavailable unless the operator both set RHEA_MCP_URL and has rhea importable.
    if "viral_conserved_sites_muscle" in by_name:
        muscle = by_name["viral_conserved_sites_muscle"]
        if not os.environ.get("RHEA_MCP_URL"):
            assert muscle["available"] is False


# ---------------------------------------------------------------------------
# Gated — the real substitution AC on live CHIKV data
# ---------------------------------------------------------------------------


@needs_both_aligners
def test_mafft_and_muscle_agree_on_conserved_regions():
    """Same CHIKV query, both aligners, conserved-region counts agree within tolerance — AND
    the two alignments differ byte-for-byte (proof the muscle path really ran MUSCLE, not a
    silent shortcut to the mafft result)."""
    from apecx_integration.composition.steps.bvbrc_protein_fasta_step import BvbrcProteinFastaStep
    from apecx_integration.composition.steps.local_mafft_align_step import LocalMafftAlignStep
    from apecx_integration.composition.steps.rhea_muscle_align_step import RheaMuscleAlignStep
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    async def _run() -> tuple[dict, dict, str, str]:
        # Fetch ONCE; align the SAME sequences with each backend (isolates the aligner variable).
        fetch = BvbrcProteinFastaStep.from_config(
            {"name": "fetch", "max_sequences": 25, "min_length_fraction": 0.8}
        )
        fout = await fetch.process({"fetch_in": dict(_QUERY)})
        fasta = fout["protein_fasta"]["fasta_text"]
        assert fasta.count(">") >= 3, "need ≥3 real CHIKV sequences for a meaningful comparison"

        m_align = (
            await LocalMafftAlignStep.from_config({"name": "m"}).process(
                {"align_in": {"fasta_text": fasta}}
            )
        )["alignment"]
        r_align = (
            await RheaMuscleAlignStep.from_config({"name": "r"}).process(
                {"align_in": {"fasta_text": fasta}}
            )
        )["alignment"]

        # And the end-to-end workflow path through each catalog entry.
        mafft_wf = await run_workflow("viral_conserved_sites", dict(_QUERY))
        muscle_wf = await run_workflow("viral_conserved_sites_muscle", dict(_QUERY))
        return m_align, r_align, mafft_wf, muscle_wf

    m_align, r_align, mafft_wf, muscle_wf = asyncio.run(_run())

    # 1) Each align step labels its own backend.
    assert m_align["aligner"] == "mafft"
    assert r_align["aligner"] == "muscle"

    # 2) SILENT-FAILURE GUARD: the alignments are genuinely different artifacts. If the muscle
    #    path shortcut to mafft, these would be byte-identical and the parity check below would
    #    be meaningless.
    assert m_align["alignment_fasta"] != r_align["alignment_fasta"], (
        "MAFFT and MUSCLE produced byte-identical alignments — the muscle path is likely NOT "
        "really dispatching to Rhea MUSCLE (silent shortcut)."
    )

    # 3) Both end-to-end runs succeed and report conserved regions.
    assert mafft_wf["status"] == "ok", mafft_wf
    assert muscle_wf["status"] == "ok", muscle_wf
    m_regions = _region_count(mafft_wf["markdown"])
    r_regions = _region_count(muscle_wf["markdown"])
    assert m_regions and m_regions > 0, mafft_wf["markdown"][:300]
    assert r_regions and r_regions > 0, muscle_wf["markdown"][:300]

    # 4) The conserved-region counts AGREE within tolerance (real biology, two real aligners on
    #    a highly-conserved protein family — they should land very close; allow ±20%).
    tol = max(3, round(0.20 * m_regions))
    assert abs(m_regions - r_regions) <= tol, (
        f"conserved-region counts diverge beyond tolerance: mafft={m_regions} muscle={r_regions} "
        f"(tol=±{tol}). mafft cols={_conserved_columns(mafft_wf['markdown'])} "
        f"muscle cols={_conserved_columns(muscle_wf['markdown'])}"
    )
