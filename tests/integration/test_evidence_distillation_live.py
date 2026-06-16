"""Live integration test for EvidenceDistillationStep on REAL retrieved records.

The unit tests exercise the ranking on synthetic dicts; this proves the same
deterministic rank-and-truncate works on the REAL record shapes that flow through
the workflow — DataCite-shaped Globus structural hits and real PubMed records —
not a hand-built fixture. Mock/integration parity for the distillation step.

Gated on Globus reachability (structural hits) + APECX_PUBMED_LIVE for the PubMed
leg (auto-skip, honest). CC-1: the happy path asserts NON-EMPTY real data.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.integration

_CHIKV_TAXON = 37124
_INDEX = "e74bf12a-d0dd-4d19-a965-03f4936db851"


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search(_INDEX, {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


needs_globus = pytest.mark.skipif(not _globus_reachable(), reason="Globus Search unreachable")
_PUBMED_LIVE = os.environ.get("APECX_PUBMED_LIVE", "").strip() == "1"


def _distill_step(tmp_path, **cfg):
    from apecx_integration.composition.steps.evidence_distillation_step import (
        EvidenceDistillationStep,
    )

    p = tmp_path / "distill.yml"
    body = "name: distill_live\n" + "".join(f"{k}: {v}\n" for k, v in cfg.items())
    p.write_text(body)
    return EvidenceDistillationStep.from_config(str(p))


def _real_structural_records() -> list[dict]:
    """Pull real CHIKV PDB hits via the same path the workflow's structural leg uses."""
    from apecx_integration.agents.globus_search import structural_query

    result = structural_query.search_one_source(
        "chikungunya structural polyprotein",
        "pdb",
        "RCSB PDB",
        taxon_id=_CHIKV_TAXON,
        species_name="Chikungunya virus",
        max_results=25,
    )
    return list(result.hits)


@needs_globus
def test_distills_real_structural_records(tmp_path):
    """Real DataCite-shaped Globus hits: digest is bounded, full list preserved, deterministic."""
    records = _real_structural_records()
    assert records, "expected non-empty real CHIKV PDB structural records (CC-1)"

    step = _distill_step(tmp_path, max_globus_results=5)
    bundle = {
        "query": "chikungunya structural polyprotein epitope",
        "globus_results": records,
    }
    out = asyncio.run(step.process(dict(bundle)))

    digest = out["globus_results"]  # replaced in place with the top-N
    assert 0 < len(digest) <= 5, f"digest must be bounded to top-5, got {len(digest)}"
    # The pre-truncation total is recorded for honest coverage.
    assert out["source_totals"]["globus_results"] == len(records)
    # Every digest entry is a real structural record (pdb:/emdb: subject).
    assert all(str(r.get("subject", "")).lower().startswith(("pdb:", "emdb:")) for r in digest)
    # Deterministic: re-running on a reversed copy yields the same ranked digest.
    out2 = asyncio.run(
        step.process({"query": bundle["query"], "globus_results": list(reversed(records))})
    )
    assert [r.get("subject") for r in digest] == [r.get("subject") for r in out2["globus_results"]]


@needs_globus
@pytest.mark.skipif(not _PUBMED_LIVE, reason="Set APECX_PUBMED_LIVE=1 for the PubMed leg")
def test_distills_real_pubmed_records(tmp_path):
    """Real PubMed records: the abstract/DOI/recency scorer truncates to top-N deterministically."""
    from apecx_integration.composition.steps import _pubmed_helpers

    pubs = asyncio.run(
        _pubmed_helpers.harvest('"Chikungunya virus" AND (epitope OR structural)', max_papers=30)
    )
    assert pubs, "expected non-empty real PubMed records (CC-1)"

    step = _distill_step(tmp_path, max_publications=8)
    out = asyncio.run(
        step.process({"query": "chikungunya epitope structural", "publications": pubs})
    )
    digest = out["publications"]  # replaced in place with the top-N
    assert 0 < len(digest) <= 8
    assert out["source_totals"]["publications"] == len(pubs)  # full count recorded
    # The note reports the real kept/total ratio.
    assert f"/{len(pubs)}" in out["distillation_note"]
