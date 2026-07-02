"""Full per-step e2e for the 3 main workflows × ≥5 entities — the durable form of the ad-hoc probing.

This is the executable, reusable version of the manual per-step verification done during the W6 arc: it
RUNS each real workflow and asserts on PER-STEP OUTPUT read from the run artifacts (not the "ok" report,
which lies — a workflow can report success while a step silently degraded; that is exactly how DF1/DF2/DF3
hid). Each check would have FAILED the corresponding bug and PASSES the fixed state.

HEAVY + OPT-IN: every workflow run is minutes (real LLM + BV-BRC + MAFFT + PyMOL / RHEA). Gated on
`APECX_RUN_WORKFLOW_E2E=1` PLUS a reachable ollama + present synonym dict, so it never runs in the normal
suite and does real work only when explicitly requested against a provisioned deployment.

Run: `APECX_RUN_WORKFLOW_E2E=1 PYTHONPATH=src:<rhea_repo> <venv>/bin/python -m pytest tests/e2e_deploy/test_workflow_steps_e2e.py -q`
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

from tests.eval.epitope_checks import check_structural_reasoning_produced


def _ollama_up() -> bool:
    base = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434").rstrip("/")
    base = base[:-3].rstrip("/") if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            return r.status == 200 and bool(json.loads(r.read()).get("models"))
    except Exception:  # noqa: BLE001
        return False


def _dict_present() -> bool:
    p = os.environ.get("APECX_SYNONYM_DICT_PATH") or str(
        Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"
    )
    return Path(p).is_file()


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("APECX_RUN_WORKFLOW_E2E") != "1",
        reason="heavy real-workflow e2e — set APECX_RUN_WORKFLOW_E2E=1 to run",
    ),
    pytest.mark.skipif(not _ollama_up(), reason="ollama not reachable / no model"),
    pytest.mark.skipif(not _dict_present(), reason="synonym dict absent (apecx-setup dict)"),
]


def _run(name: str, params: dict) -> tuple[dict, Path]:
    """Run a workflow via the internal runner (returns the envelope incl. artifact_dir) + the dir."""
    from apecx_integration.mcp_surface.tools.eo_primitives import run_workflow

    res = asyncio.run(run_workflow(name, params))
    ad = res.get("artifact_dir") or res.get("artifact_path") or ""
    return res, Path(ad)


def _report_text(res: dict, artifact_dir: Path) -> str:
    txt = res.get("_text") or res.get("markdown") or ""
    if not txt and (artifact_dir / "report.md").is_file():
        txt = (artifact_dir / "report.md").read_text()
    return txt


# --------------------------------------------------------- W1: viral_epitope_analysis (structural SASA)

# Surface antigens on taxa with deposited PDB structures — each MUST produce real per-residue SASA.
# SARS-CoV-2 spike is deliberately EXCLUDED (DF3b: whole-length-region vs EMDB-dominated structures → 0
# exposed; tracked separately, xfail below) so this parametrization is the set that must be green.
_STRUCT_ENTITIES = [
    "chikungunya virus E1",
    "influenza A virus hemagglutinin",
    "Zika virus envelope",
    "dengue virus envelope",
    "HIV-1 gp120",
]


@pytest.mark.parametrize("query", _STRUCT_ENTITIES)
def test_viral_epitope_produces_real_sasa(query):
    res, ad = _run("viral_epitope_analysis", {"query": query})
    assert res.get("error") is None, f"{query}: {res.get('error')}"
    check = check_structural_reasoning_produced(ad, expect_structure=True)
    assert check.passed, f"{query}: structural analysis produced no SASA — {check.evidence}"


@pytest.mark.xfail(
    reason="DF3b: spike whole-length region vs EMDB-dominated structures → n_exposed=0",
    strict=False,
)
def test_viral_epitope_spike_df3b():
    _res, ad = _run("viral_epitope_analysis", {"query": "SARS-CoV-2 spike"})
    assert check_structural_reasoning_produced(ad, expect_structure=True).passed


# --------------------------------------------------------- W2: rag_e2e_synthesis (grounded synthesis)

_RAG_QUERIES = [
    "What conserved epitopes exist on chikungunya virus E1 glycoprotein?",
    "What VIOLIN vaccine records and BV-BRC genomes exist for Zika virus?",
    "Summarize dengue virus vaccine candidates and their targets.",
    "What is known about SARS-CoV-2 spike neutralizing antibody epitopes?",
    "What antiviral drug targets exist for influenza A virus?",
]

_CITE = re.compile(r"\[BV-BRC|\[VIOLIN|\[RAG chunk|\[Globus|\[10\.\d|DOI", re.IGNORECASE)


@pytest.mark.parametrize("query", _RAG_QUERIES)
def test_rag_e2e_synthesis_is_grounded(query):
    res, ad = _run("rag_e2e_synthesis", {"query": query})
    assert res.get("status") in {"ok", "partial"}, f"{query}: status={res.get('status')}"
    md = _report_text(res, ad)
    assert len(md) > 400, f"{query}: synthesis too short ({len(md)})"
    cites = len(_CITE.findall(md))
    assert cites >= 3, f"{query}: synthesis not grounded (only {cites} citation markers)"


# --------------------------------------------------------- W3: rhea_muscle_alignment (real alignment)

_rhea_ready = (
    os.environ.get("RHEA_MCP_URL") is not None and importlib.util.find_spec("rhea") is not None
)


@pytest.mark.skipif(
    not _rhea_ready,
    reason="needs the rhea module importable + RHEA_MCP_URL set (apecx-setup rhea; PYTHONPATH=../rhea)",
)
def test_rhea_muscle_alignment_produces_alignment():
    res, ad = _run("rhea_muscle_alignment", {})  # bundled default FASTA
    assert res.get("error") is None, f"muscle: {res.get('error')}"
    wo = {}
    p = ad / "tool_outputs" / "workflow_output.json"
    if p.is_file():
        wo = json.loads(p.read_text())
    assert (wo.get("n_sequences") or 0) >= 2, f"expected >=2 aligned sequences: {wo!r}"
    assert len(wo.get("alignment_fasta") or "") > 0, "empty alignment_fasta"
