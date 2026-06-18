"""Real-chain probe for epitope_combination_feasibility_assessment.

Drives the FULL three-workflow chain against real backends (BV-BRC + MAFFT + LLM):

    viral_epitope_analysis  ->  conserved_epitope_candidate_assessment  ->
    epitope_combination_feasibility_assessment

for viruses with deliberately DIFFERENT strain counts (a heavily-sequenced virus vs a
sparsely-sequenced one). The point is parity: the combination step must read the REAL
upstream candidate-assessment bundle shape — not a fabricated fixture — and behave
correctly across that diversity.

Success is decided from OUTPUT VALUES, never from ``status`` (G127): a run that returns
``status: ok`` with empty/None markdown is a silent failure, and the assertions below
would catch it. Both real outcomes are exercised:
  * candidate released  -> combination releases the assessment (after approval);
  * candidate withheld  -> combination emits a graceful needs_input terminal that
    survives the intake->classify->release pipeline unchanged.
"""

from __future__ import annotations

import asyncio
import re

import pytest

pytestmark = pytest.mark.integration


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search("e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


needs_globus = pytest.mark.skipif(
    not _globus_reachable(), reason="needs reachable Globus Search for the upstream evidence run"
)

# A fixed, plausible additional epitope. The combination workflow catalogs supplied epitopes;
# it does not validate biological relatedness, so a fixed peptide isolates the variable under
# test (the REAL candidate handle, which varies with upstream strain count).
_ADDITIONAL_EPITOPES = [
    {
        "label": "reported additional epitope",
        "sequence": "GILGFVFTL",
        "start": 200,
        "end": 208,
        "source": "reported literature epitope",
        "reported_recognition_evidence": {"class": "reported"},
    }
]

# (query, protein, label) — chosen for a sharp strain-count contrast.
_VIRUSES = [
    (
        "dengue virus envelope protein conserved epitopes",
        "envelope protein",
        "dengue (many strains)",
    ),
    ("Mayaro virus E2 conserved epitopes", "E2", "mayaro (few strains)"),
]


@pytest.fixture(autouse=True)
def _clean_stores():
    from apecx_integration.composition.handles.store import default_handle_store
    from apecx_integration.composition.runtime.design_approval_store import (
        get_design_approval_store,
    )
    from apecx_integration.mcp_surface.workflow_registry import _clear_workflow_cache

    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()
    yield
    get_design_approval_store().clear()
    default_handle_store().clear()
    _clear_workflow_cache()


def _approve_and_rerun(tool: str, params: dict) -> dict:
    """Run a gated workflow; if it withholds on a design-approval token, approve and re-run.

    Returns the final output dict. A needs_input WITHOUT a dapprv- token (e.g. a missing
    prerequisite) is returned as-is — the caller decides what that means.
    """
    from apecx_integration.mcp_surface.tools.eo_primitives import approve_design, run_workflow

    first = asyncio.run(run_workflow(tool, params))
    if first.get("status") == "ok":
        return first
    md = first.get("markdown") or ""
    m = re.search(r"dapprv-[a-f0-9]+", md)
    if not m:
        return first
    assert approve_design(m.group(0)).get("status") == "approved", first
    return asyncio.run(run_workflow(tool, {**params, "design_approval_id": m.group(0)}))


def _candidate_sequence(candidate_handle: str) -> str | None:
    from apecx_integration.composition.handles.store import default_handle_store

    shape = default_handle_store().get(candidate_handle)
    parts = getattr(shape, "parts", {}) or {}
    cand = parts.get("candidate") or {}
    return cand.get("sequence")


@needs_globus
@pytest.mark.parametrize("query,protein,label", _VIRUSES, ids=[v[2] for v in _VIRUSES])
def test_full_chain_combination_across_strain_counts(query, protein, label):
    """Both real paths, per virus, against a REAL candidate bundle (one expensive upstream run):
    (1) the candidate-not-released DEGRADE path (an unapproved bundle has
    candidate_released=False) -> combination must emit a graceful needs_input terminal that
    survives the intake->classify->release pipeline; (2) the RELEASE path (approved candidate)
    -> combination releases the assessment. Success is decided on OUTPUT VALUES, not status.
    """
    upstream = asyncio.run(
        _run_workflow_named("viral_epitope_analysis", {"query": query, "protein": protein})
    )
    # The upstream is RELIABILITY-status-ok by design even on a no-hit; decide on the handle.
    assert upstream["status"] == "ok", (label, upstream)
    assert upstream["data_handle"], (label, upstream)
    evidence_handle = upstream["data_handle"]

    # (1) DEGRADE PATH on real data: an UNAPPROVED candidate-assessment bundle carries
    # candidate_released=False. Feeding it to the combination must NOT silently release —
    # it must terminate as needs_input, and that terminal must survive both downstream steps.
    unapproved = asyncio.run(
        _run_workflow_named(
            "conserved_epitope_candidate_assessment", {"evidence_data_handle": evidence_handle}
        )
    )
    assert unapproved["status"] == "needs_input", (label, unapproved)
    assert unapproved["data_handle"], (label, unapproved)
    degraded = asyncio.run(
        _run_workflow_named(
            "epitope_combination_feasibility_assessment",
            {
                "evidence_data_handle": evidence_handle,
                "candidate_assessment_handle": unapproved["data_handle"],
                "additional_epitopes": _ADDITIONAL_EPITOPES,
            },
        )
    )
    assert degraded["status"] == "needs_input", (label, degraded)
    assert "no released candidate" in (degraded["markdown"] or "").lower(), (label, degraded)

    # (2) RELEASE PATH: approve the candidate, then the combination.
    candidate = _approve_and_rerun(
        "conserved_epitope_candidate_assessment", {"evidence_data_handle": evidence_handle}
    )
    if candidate.get("status") != "ok":
        # Genuinely too sparse to release a candidate even with approval; the degrade path
        # above already proved graceful handling. Nothing more to assert for this virus.
        assert candidate.get("status") == "needs_input", (label, candidate)
        return

    cand_handle = candidate["data_handle"]
    assert cand_handle, (label, candidate)
    cand_seq = _candidate_sequence(cand_handle)
    assert cand_seq, (label, "real candidate bundle carried no candidate sequence")

    combo = _approve_and_rerun(
        "epitope_combination_feasibility_assessment",
        {
            "evidence_data_handle": evidence_handle,
            "candidate_assessment_handle": cand_handle,
            "additional_epitopes": _ADDITIONAL_EPITOPES,
        },
    )
    assert combo["status"] == "ok", (label, combo)
    assert combo["error"] is None, (label, combo)
    md = combo["markdown"] or ""
    # Decide success on OUTPUT VALUES, not status.
    assert "## Summary" in md and "## Epitopes" in md, (label, md[:1500])
    assert cand_seq in md, (label, "real candidate sequence missing from released combination")
    assert "reported additional epitope" in md, (label, md[:1500])
    assert combo["data_preview"]["kind"] == "bundle", (label, combo)


async def _run_workflow_named(tool: str, params: dict) -> dict:
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    return await run_workflow(tool, params)
