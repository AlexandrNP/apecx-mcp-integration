"""viral_epitope_evidence_review (Track D) — the evidence workflow as a catalog tool.

Proves the lightweight WorkflowBuilder catalog entry: builds with real child steps (guards the
0-child-steps silent failure), is registered + listed, gates missing params via RoC-2c, and —
gated on a reachable LLM + Globus — runs end-to-end through `run_workflow` to a real
WorkflowResult whose markdown ALWAYS carries a Structural evidence section.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest
import requests

pytestmark = pytest.mark.integration

_CHIKV_TAXON = 37124


def _globus_reachable() -> bool:
    try:
        import globus_sdk

        c = globus_sdk.SearchClient()
        c.post_search("e74bf12a-d0dd-4d19-a965-03f4936db851", {"q": "*", "limit": 0})
        return True
    except Exception:
        return False


def _llm_reachable() -> bool:
    """True only when the endpoint is up AND the configured chat model is actually
    available. A reachable endpoint with the model un-pulled must SKIP (honest), not
    fail — the model name is resolved exactly as ``build_chat_llm`` resolves it."""
    # Mirror _llm_factory.build_chat_llm's defaults exactly.
    base = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    model = os.environ.get("APECX_LLM_MODEL", "nemotron-3-nano:4b")
    stem = model.split(":", 1)[0]
    # The openai-compat base ends in /v1; Ollama's native model list is at the ROOT.
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base

    def _has_model(url: str, list_key: str, name_key: str) -> bool:
        try:
            r = requests.get(url, timeout=3)
            if not r.ok:
                return False
            names = [it.get(name_key, "") for it in r.json().get(list_key, [])]
            return any(n == model or n.split(":", 1)[0] == stem for n in names)
        except Exception:
            return False

    # Ollama-native (/api/tags) OR OpenAI-compat (/v1/models) — try both forms.
    return (
        _has_model(root + "/api/tags", "models", "name")
        or _has_model(root + "/v1/models", "data", "id")
        or _has_model(base + "/models", "data", "id")
    )


needs_llm_and_globus = pytest.mark.skipif(
    not (_llm_reachable() and _globus_reachable()),
    reason="needs a reachable LLM endpoint (APECX_LLM_*) AND Globus Search",
)

# The fan-in / design-gate proof tests exercise the AllDataReceivedTrigger re-fire
# and the gate's needs_input/approval logic — none of which touch Globus. The
# structural leg degrades LOUD (renders its section header with an outage/no-hit
# note) when Globus is unreachable, so these run on LLM alone. Gating them on Globus
# too would leave the fan-in fix unverified whenever Globus is flaky.
needs_llm = pytest.mark.skipif(
    not _llm_reachable(),
    reason="needs a reachable LLM endpoint (APECX_LLM_*)",
)


def _pymol_image_present() -> bool:
    """True when the version-pinned containerized PyMOL image is built locally (E2-P)."""
    if shutil.which("docker") is None:
        return False
    try:
        import subprocess

        return (
            subprocess.run(
                ["docker", "image", "inspect", "apecx-pymol:3.1.0"],
                capture_output=True,
                timeout=20,
            ).returncode
            == 0
        )
    except Exception:
        return False


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


# The sequence-conservation leg (E2-C1) nests viral_conserved_sites: it needs a local MAFFT
# binary AND a reachable BV-BRC, on top of the LLM, to prove the HAPPY path end-to-end.
needs_llm_seq = pytest.mark.skipif(
    not (_llm_reachable() and shutil.which("mafft") is not None and _bvbrc_reachable()),
    reason="needs a reachable LLM (APECX_LLM_*) AND MAFFT installed AND BV-BRC reachable",
)

_QUERY = "conserved chikungunya structural polyprotein epitopes and structural references"


# --------------------------- no-network structural guards ---------------------------
def test_builder_produces_workflow_with_child_steps():
    """Guards the WorkflowBuilder 0-child-steps silent failure (loads with 0 steps → silent
    no_first_step). No network needed — construction only."""
    from apecx_integration.composition.workflows.viral_epitope_evidence_review.builder import (
        build_viral_epitope_evidence_review_workflow,
    )

    wf = build_viral_epitope_evidence_review_workflow()
    children = getattr(wf, "child_steps", None) or getattr(wf, "_child_steps", None)
    assert isinstance(children, dict)
    assert set(children) == {
        "normalize",
        "assemble",
        "structural",
        "sequence",
        "merge",
        "reasoning",
        "review",
        "gate",
        "envelope",
    }


def test_registered_in_catalog_and_listed():
    from apecx_integration.mcp_surface.tools.discovery import list_workflows
    from apecx_integration.mcp_surface.workflow_registry import load_catalog

    names = {e.tool_name for e in load_catalog().workflows}
    assert "viral_epitope_evidence_review" in names

    out = asyncio.run(list_workflows())
    row = next(r for r in out["runnable"] if r["name"] == "viral_epitope_evidence_review")
    assert row["invoke_with"] == "run_workflow"
    assert isinstance(row["available"], bool)


def test_missing_query_returns_needs_input():
    """RoC-2c: missing required `query` → needs_input BEFORE any backend call."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(run_workflow("viral_epitope_evidence_review", {"protein": "E1"}))
    assert out["status"] == "needs_input", out
    ct = out["control_transfer"]
    assert ct["reason"] == "missing_param"
    params = ct["next_action"]["params"]
    assert any(p["param_name"] == "query" and p["issue"] == "missing" for p in params)
    assert out["data_handle"] is None  # did not run


# --------------------------- end-to-end (real LLM + Globus) ---------------------------
@needs_llm_and_globus
def test_evidence_only_e2e_has_structural_section():
    """End-to-end against real Globus + LLM. RELIABILITY: status is always ok — even if
    the LLM's narrative fails the citation gate, the step degrades to a deterministic
    evidence summary rather than discarding the retrieved evidence."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(run_workflow("viral_epitope_evidence_review", {"query": _QUERY}))
    assert out["status"] == "ok", out
    assert out["error"] is None
    assert out["run_id"]
    md = out["markdown"]
    assert md and md.strip()
    # The structural section is ALWAYS present — records or an explicit no-hit (no silent omission).
    assert "## Structural evidence" in md, md[:2000]


@needs_llm_and_globus
def test_evidence_output_contract_five_sections_e2e():
    """OUTPUT CONTRACT (E2-B): the final Markdown carries the five contract sections,
    in order, and the deterministic Sources section lists at least one REAL cited
    record with a non-'(untitled)' title (DataCite title resolution end-to-end)."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(run_workflow("viral_epitope_evidence_review", {"query": _QUERY}))
    assert out["status"] == "ok", out
    md = out["markdown"]
    assert md and md.strip()

    headers = [
        "# Answer",
        "## Cross-data reasoning",
        "## Integrated insight",
        "## Sources and evidence",
        "## Follow-up questions",
    ]
    positions = [md.find(h) for h in headers]
    assert all(p != -1 for p in positions), (positions, md[:3000])
    assert positions == sorted(positions), (positions, md[:3000])

    # Sources lists at least one real cited record with a resolved (non-untitled) title.
    sources_block = md[md.find("## Sources and evidence") : md.find("## Follow-up questions")]
    bullet_lines = [ln for ln in sources_block.splitlines() if ln.startswith("- **[")]
    assert bullet_lines, sources_block
    assert any("*(untitled)*" not in ln for ln in bullet_lines), sources_block

    # The reasoning-trace scaffolding surfaced both wired stages.
    assert "### Reasoning trace" in md
    assert "context_assembly" in md and "structural_evidence" in md


@needs_llm_and_globus
def test_structural_no_hit_is_named_e2e(monkeypatch):
    """With the Globus branch disabled, the structural leg MUST emit the loud no-hit line —
    proving the no-silent-failure path end-to-end (not just at the unit level)."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    # Disable the Globus branch so structural lookup deterministically finds nothing.
    monkeypatch.setenv("APECX_GLOBUS_SEARCH_DISABLED", "1")
    out = asyncio.run(
        run_workflow("viral_epitope_evidence_review", {"query": "Mayaro virus nsP2 protease"})
    )
    # Degrade-loud guarantees a result; the structural section names the no-hit explicitly.
    assert out["status"] == "ok", out
    assert "No PDB or EMDB structural records" in out["markdown"], out["markdown"][:2000]


@needs_llm_seq
def test_sequence_conservation_stage_e2e():
    """E2-C1 END-TO-END (the apecx-side integration gap E2-F1 flagged): the full evidence
    workflow, on a real CHIKV query WITH taxon_id + protein, nests viral_conserved_sites
    (BV-BRC fetch → MAFFT MSA → conservation), folds the structured conservation into the
    bundle, and surfaces a `sequence_conservation` stage report with REAL conserved regions
    in the reasoning trace — alongside the unchanged 5-section output contract."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow(
            "viral_epitope_evidence_review",
            {"query": _QUERY, "taxon_id": _CHIKV_TAXON, "protein": "structural polyprotein"},
        )
    )
    assert out["status"] == "ok", out
    md = out["markdown"]
    assert md and md.strip()

    # The five-section output contract is intact (the sequence stage did not perturb it).
    headers = [
        "# Answer",
        "## Cross-data reasoning",
        "## Integrated insight",
        "## Sources and evidence",
        "## Follow-up questions",
    ]
    positions = [md.find(h) for h in headers]
    assert all(p != -1 for p in positions), (positions, md[:3000])
    assert positions == sorted(positions), (positions, md[:3000])

    # The sequence-conservation stage report is present in the reasoning trace…
    assert "### Reasoning trace" in md
    seq_lines = [ln for ln in md.splitlines() if "sequence_conservation" in ln]
    assert seq_lines, ("no sequence_conservation stage report rendered", md[:4000])
    seq_line = seq_lines[0]
    # …carrying REAL conserved regions (the happy path), not the loud degrade note.
    assert "Sequence conservation unavailable" not in seq_line, (
        "sequence leg degraded — expected real conserved sites for CHIKV structural polyprotein",
        seq_line,
    )
    assert "conserved region(s) at" in seq_line, seq_line

    # E2-P: when the containerized PyMOL image is present, the structural-reasoning stage
    # also surfaces in the trace (real mapped structure or a loud degrade note). It is
    # additive and must NEVER break the 5-section contract above.
    if _pymol_image_present():
        sr_lines = [ln for ln in md.splitlines() if "structural_reasoning" in ln]
        assert sr_lines, ("no structural_reasoning stage report rendered", md[:4000])
    assert "per-strain sequences" in seq_line, seq_line


@needs_llm
def test_design_without_approval_returns_needs_input_e2e():
    """FAN-IN PROOF: requested_outputs=evidence_plus_design without a design_approval_id
    must reach the gate (via the AllDataReceivedTrigger fan-in) and return needs_input —
    proving the lightweight fan-in fires at runtime, not just loads."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow(
            "viral_epitope_evidence_review",
            {"query": _QUERY, "requested_outputs": "evidence_plus_design"},
        )
    )
    assert out["status"] == "needs_input", out
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    # Evidence is NOT discarded on the pause — the gate still returns the gathered evidence.
    assert "## Structural evidence" in out["markdown"], out["markdown"][:2000]
    assert "WITHHELD" in out["markdown"]


@needs_llm
def test_design_with_approval_appends_design_section_e2e():
    """FAN-IN PROOF (approved path): with a design_approval_id the gate opens and appends
    the design-hypotheses section carrying approval provenance."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    out = asyncio.run(
        run_workflow(
            "viral_epitope_evidence_review",
            {
                "query": _QUERY,
                "requested_outputs": "evidence_plus_design",
                "design_approval_id": "appr-e2e-001",
            },
        )
    )
    assert out["status"] == "ok", out
    assert "Design / optimization hypotheses (approved)" in out["markdown"], out["markdown"][:2000]
    assert "appr-e2e-001" in out["markdown"]  # approval provenance carried through
